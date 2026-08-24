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


@dataclass
class DualPipelineResult:
    tier_10: PipelineResult
    tier_20: PipelineResult
    filter_stats: FilterStats

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier_10": self.tier_10.to_dict(),
            "tier_20": self.tier_20.to_dict(),
            "total_screened": self.filter_stats.total,
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
            "status": {
                "type": m.status_type,
                "description": m.status_description,
            },
            "status_type": m.status_type,
            "startTimestamp": m.start_timestamp,
            "start_time_utc": m.start_time.isoformat() if m.start_time else "",
            "start_time_wat": str(m.start_time),
            "home_score": m.home_score,
            "away_score": m.away_score,
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
        logger.info("Starting prediction pipeline with %d raw fixtures (top_n=%d)...", len(raw_matches), top_n)

        # 1. Filter tradeable competitive fixtures
        fixtures, stats = filter_fixtures(raw_matches, allow_unlisted=True)
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
            limit=max(top_n * 2, 40),
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

    async def run_dual_pipeline(
        self,
        raw_matches: list[dict[str, Any]],
        form_matches: int = 10,
        auto_book: bool = True,
    ) -> DualPipelineResult:
        """
        Generate BOTH Top 10 Bankers and Top 20 Mega Accumulator tickets simultaneously.
        """
        logger.info("Running dual prediction pipeline (Top 10 & Top 20) with %d raw fixtures...", len(raw_matches))
        fixtures, stats = filter_fixtures(raw_matches, allow_unlisted=True)
        if not fixtures:
            empty = PipelineResult(picks=[], filter_stats=stats, picks_text="")
            return DualPipelineResult(tier_10=empty, tier_20=empty, filter_stats=stats)

        # Pre-filter against SportyBet active fixtures
        if auto_book:
            sporty_events = await self.booker.service.fetch_available_events()
            if sporty_events:
                from core.team_matcher import match_fixture
                bookable_fixtures = [
                    fx for fx in fixtures
                    if match_fixture(fx.home_name, fx.away_name, sporty_events, threshold=0.48)
                ]
                if bookable_fixtures:
                    fixtures = bookable_fixtures

        # Fetch form once for all matches
        forms = await fetch_team_forms(fixtures, form_matches=form_matches)

        # Screen up to 35 picks
        all_screened = screen_fixtures(fixtures=fixtures, forms=forms, limit=35, max_per_fixture=1)
        if not all_screened:
            empty = PipelineResult(picks=[], filter_stats=stats, picks_text="")
            return DualPipelineResult(tier_10=empty, tier_20=empty, filter_stats=stats)

        # Top 10 Picks
        picks_10 = all_screened[:10]
        picks_text_10 = format_picks(picks_10)
        book_res_10 = None
        if auto_book and picks_10:
            book_res_10 = await self.booker.book_predictions(picks_text_10)

        tier_10 = PipelineResult(
            picks=picks_10,
            filter_stats=stats,
            picks_text=picks_text_10,
            booking_result=book_res_10,
        )

        # Top 20 Picks
        picks_20 = all_screened[:20]
        picks_text_20 = format_picks(picks_20)
        book_res_20 = None
        if auto_book and picks_20:
            book_res_20 = await self.booker.book_predictions(picks_text_20)

        tier_20 = PipelineResult(
            picks=picks_20,
            filter_stats=stats,
            picks_text=picks_text_20,
            booking_result=book_res_20,
        )

        return DualPipelineResult(tier_10=tier_10, tier_20=tier_20, filter_stats=stats)

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

    @staticmethod
    def format_telegram_dual_digest(dual_res: DualPipelineResult, date_str: str) -> str:
        """Format both Top 10 Bankers and Top 20 Mega Accumulator tickets into a dual summary."""
        lines = [
            f"<b>🎯 Daily AI Predictions & Auto-Booked Tickets ({date_str})</b>\n",
            f"🧠 <i>Screened {dual_res.filter_stats.total} fixtures across 75+ elite competitions</i>\n",
            "━━━━━━━━━━━━━━━━━━━━",
            "🎯 <b>TICKET 1: TOP 10 BANKERS</b> (High Conviction)",
        ]

        if dual_res.tier_10.booking_result and dual_res.tier_10.booking_result.success:
            b10 = dual_res.tier_10.booking_result
            lines.extend([
                f"🎟️ <b>SportyBet Code:</b> <code>{b10.booking_code}</code> <i>(tap to copy)</i>",
                f"📊 <b>Total Odds:</b> <b>{b10.total_odds or '—'}</b> | ⚽ <b>{len(dual_res.tier_10.picks)} Games</b>",
            ])
        lines.append("")

        for idx, p in enumerate(dual_res.tier_10.picks, 1):
            lines.append(f"<b>{idx}.</b> {p.fixture.home_name} vs {p.fixture.away_name} ➔ <b>{p.selection}</b> <i>({p.market}, {round(p.probability * 100)}%)</i>")

        lines.extend([
            "\n━━━━━━━━━━━━━━━━━━━━",
            "🚀 <b>TICKET 2: TOP 20 MEGA ACCUMULATOR</b> (Extended Slip)",
        ])

        if dual_res.tier_20.booking_result and dual_res.tier_20.booking_result.success:
            b20 = dual_res.tier_20.booking_result
            lines.extend([
                f"🎟️ <b>SportyBet Code:</b> <code>{b20.booking_code}</code> <i>(tap to copy)</i>",
                f"📊 <b>Total Odds:</b> <b>{b20.total_odds or '—'}</b> | ⚽ <b>{len(dual_res.tier_20.picks)} Games</b>",
            ])
        lines.append("")

        for idx, p in enumerate(dual_res.tier_20.picks, 1):
            lines.append(f"<b>{idx}.</b> {p.fixture.home_name} vs {p.fixture.away_name} ➔ <b>{p.selection}</b> <i>({p.market})</i>")

        return "\n".join(lines)
