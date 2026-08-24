"""
Automated Prediction & Booking Pipeline for SportCrawl.

Coordinates:
1. Fixture loading (from DB or live SofaScore scraping)
2. Statistical screening & form enrichment (via core.predictor)
3. Automated SportyBet betslip booking (via BookerEngine)
4. Rich summary generation for Telegram digests and Web UI
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from core.booker_engine import BookerEngine
from core.predictor.enrich import fetch_team_forms
from core.predictor.filter import FilterStats, Fixture, filter_fixtures
from core.predictor.format import format_picks
from core.predictor.screen import Pick, screen_fixtures
from services.sportybet_service import BookingResult
from storage.database import DatabaseManager
from storage.models import FootballMatch

logger = logging.getLogger("SportCrawl.Pipeline")


@dataclass
class PipelineResult:
    picks: list[Pick]
    filter_stats: FilterStats
    picks_text: str
    booking_result: Optional[BookingResult] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_screened": self.filter_stats.total,
            "tradeable": self.filter_stats.kept,
            "picks_count": len(self.picks),
            "picks": [
                {
                    "home_team": p.fixture.home_name,
                    "away_team": p.fixture.away_name,
                    "tournament": p.fixture.tournament,
                    "category": p.fixture.category,
                    "market": p.market,
                    "selection": p.selection,
                    "probability": round(p.probability * 100, 1),
                    "conviction": round(p.conviction * 100, 1),
                    "rationale": p.rationale,
                    "line": p.line,
                }
                for p in self.picks
            ],
            "picks_text": self.picks_text,
            "booking": self.booking_result.to_dict() if self.booking_result else None,
        }


def convert_matches_to_raw_dicts(matches: list[FootballMatch]) -> list[dict[str, Any]]:
    """Convert Database FootballMatch model instances into the dictionary schema expected by filter_fixtures."""
    raw_list = []
    for m in matches:
        raw_list.append({
            "match_id": m.match_id,
            "tournament": m.tournament_name,
            "category": m.category_name,
            "home_team": {
                "id": m.home_team_id,
                "name": m.home_team,
            },
            "away_team": {
                "id": m.away_team_id,
                "name": m.away_team,
            },
            "status_type": m.status_type,
            "start_time_utc": m.start_time.isoformat() if m.start_time else "",
            "start_time_wat": str(m.start_time),
        })
    return raw_list


class PredictionBookingPipeline:
    """End-to-end pipeline: Scrape/DB -> Filter -> Enrich -> Screen -> Auto-Book -> Notify."""

    def __init__(self, country_code: str = "ng", headless: bool = True):
        self.country_code = country_code
        self.headless = headless
        self.booker = BookerEngine(country_code=country_code, headless=headless)

    async def run_pipeline(
        self,
        raw_matches: list[dict[str, Any]],
        top_n: int = 10,
        form_matches: int = 10,
        auto_book: bool = True,
    ) -> PipelineResult:
        """
        Execute prediction screening on fixtures and automatically book them on SportyBet.
        """
        logger.info("Starting prediction pipeline with %d raw fixtures...", len(raw_matches))

        # 1. Filter tradeable competitive fixtures
        fixtures, stats = filter_fixtures(raw_matches, allow_unlisted=False)
        if not fixtures:
            logger.warning("No tradeable fixtures survived filtering.")
            return PipelineResult(picks=[], filter_stats=stats, picks_text="")

        # 2. Pre-filter against currently active SportyBet events if auto-booking
        if auto_book:
            logger.info("Checking available SportyBet fixtures to ensure 100% bookability...")
            sporty_events = await self.booker.service.fetch_available_events()
            if sporty_events:
                from core.team_matcher import match_fixture
                bookable_fixtures = []
                for fx in fixtures:
                    if match_fixture(fx.home_name, fx.away_name, sporty_events, threshold=0.48):
                        bookable_fixtures.append(fx)
                if bookable_fixtures:
                    logger.info("Matched %d/%d fixtures available on SportyBet", len(bookable_fixtures), len(fixtures))
                    fixtures = bookable_fixtures
                else:
                    logger.warning("No SofaScore fixtures matched SportyBet's active event list.")

        # 3. Enrich team forms via SofaScore API
        logger.info("Enriching %d tradeable fixtures with recent form...", len(fixtures))
        forms = await fetch_team_forms(fixtures, form_matches=form_matches)

        # 4. Screen fixtures using statistical Poisson goals model
        all_screened = screen_fixtures(
            fixtures=fixtures,
            forms=forms,
            limit=top_n * 2,
            max_per_fixture=1,
        )
        logger.info("Screened %d qualifying picks", len(all_screened))

        if not all_screened:
            return PipelineResult(picks=[], filter_stats=stats, picks_text="")

        picks = all_screened[:top_n]
        picks_text = format_picks(picks)

        booking_res = None
        # 5. Automatically book on SportyBet
        if auto_book:
            logger.info("Auto-booking %d picks on SportyBet...", len(picks))
            booking_res = await self.booker.book_predictions(picks_text)
            if booking_res.success and booking_res.booked_selections:
                logger.info("✅ SportyBet Booking Success! Code: %s (Odds: %s)", booking_res.booking_code, booking_res.total_odds)
                from core.team_matcher import team_similarity
                aligned_picks = []
                for p in picks:
                    is_booked = any(
                        team_similarity(p.fixture.home_name, s.home_team) >= 0.5
                        and team_similarity(p.fixture.away_name, s.away_team) >= 0.5
                        for s in booking_res.booked_selections
                    )
                    if is_booked:
                        aligned_picks.append(p)
                if aligned_picks:
                    picks = aligned_picks
            else:
                logger.warning("SportyBet booking warning: %s", booking_res.error_message if booking_res else "None")

        return PipelineResult(
            picks=picks,
            filter_stats=stats,
            picks_text=picks_text,
            booking_result=booking_res,
        )

    @staticmethod
    def format_telegram_digest(result: PipelineResult, title: str = "🎯 Daily Top Banker Predictions") -> str:
        """Format the prediction pipeline output for Telegram with rich HTML."""
        if not result.picks:
            return f"<b>{title}</b>\n\n<i>No matches cleared today's strict conviction threshold (>62% probability).</i>"

        lines = [
            f"<b>{title}</b>\n",
            f"🧠 <i>Screened {result.filter_stats.total} fixtures ➔ {len(result.picks)} high-conviction picks</i>\n",
        ]

        if result.booking_result and result.booking_result.success:
            code = result.booking_result.booking_code
            odds = result.booking_result.total_odds or "—"
            lines.extend([
                f"🎟️ <b>SportyBet Booking Code:</b> <code>{code}</code> <i>(tap to copy)</i>",
                f"📊 <b>Total Odds:</b> <b>{odds}</b>",
                f"⚽ <b>Games:</b> <b>{len(result.picks)}</b>\n",
                "<b>📋 Recommended Selections:</b>",
            ])
        else:
            lines.append("<b>📋 Top Statistical Picks:</b>")

        for idx, p in enumerate(result.picks, 1):
            prob_pct = f"{round(p.probability * 100)}%"
            conv_pct = f"{round(p.conviction * 100)}%"
            lines.append(
                f"\n<b>{idx}. {p.fixture.home_name} vs {p.fixture.away_name}</b>\n"
                f"   └ 🎯 <b>{p.selection}</b> ({p.market})\n"
                f"   └ 📈 Probability: <b>{prob_pct}</b> (Conviction: {conv_pct})\n"
                f"   └ 💡 <i>{p.rationale}</i>"
            )

        if result.booking_result and result.booking_result.share_url:
            lines.append(f'\n🔗 <a href="{result.booking_result.share_url}">Open Betslip on SportyBet ↗</a>')

        return "\n".join(lines)
