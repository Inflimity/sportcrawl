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
from core.predictor.odds import PricedPick
from core.predictor.screen import Pick, screen_fixtures
from core.predictor.tickets import Ticket
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
class DrawTicket:
    """One booked draw accumulator: its legs, its price, and its code."""

    ticket: "Ticket"
    booking_result: Optional[BookingResult] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.ticket.label,
            "legs": len(self.ticket.legs),
            "combined_probability": round(self.ticket.combined_probability * 100, 4),
            "combined_odds": (
                round(self.ticket.combined_odds, 2) if self.ticket.combined_odds else None
            ),
            "expected_hits_per_year": round(self.ticket.expected_hits_per_year, 3),
            "lines": self.ticket.lines,
            "booking": self.booking_result.to_dict() if self.booking_result else None,
        }


@dataclass
class DrawPipelineResult:
    """
    The daily draw ladder: a 10-fold plus two five-folds, each booked separately.

    Kept apart from PipelineResult because the two tracks are not comparable.
    The Top 10/20 picks run at 75-85% a leg; these run near 30%, so a shared
    summary would invite reading one as evidence about the other.
    """

    picks: list[Pick]
    tickets: list[DrawTicket]
    screen_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "picks_count": len(self.picks),
            "screen_summary": self.screen_summary,
            "tickets": [t.to_dict() for t in self.tickets],
        }


@dataclass
class TwoOddsResult:
    """
    The short banker: at most three legs, priced to land at or under 2.00.

    Separate from the Top 10/20 tickets because it answers a different
    question. Those maximise conviction and let the price fall where it may;
    this one fixes the price first and takes the best legs that fit under it.
    """

    ticket: Optional["Ticket"] = None
    booking_result: Optional[BookingResult] = None
    max_combined_odds: float = 2.0
    source: str = "form"

    def to_dict(self) -> dict[str, Any]:
        if not self.ticket:
            return {"built": False, "max_combined_odds": self.max_combined_odds,
                    "source": self.source}
        return {
            "built": True,
            "max_combined_odds": self.max_combined_odds,
            "source": self.source,
            "legs": len(self.ticket.legs),
            "combined_probability": round(self.ticket.combined_probability * 100, 2),
            "combined_odds": (
                round(self.ticket.combined_odds, 2) if self.ticket.combined_odds else None
            ),
            "expected_value": (
                round(self.ticket.expected_value, 3) if self.ticket.expected_value else None
            ),
            "lines": self.ticket.lines,
            "booking": self.booking_result.to_dict() if self.booking_result else None,
        }


@dataclass
class DualPipelineResult:
    tier_10: PipelineResult
    tier_20: PipelineResult
    filter_stats: FilterStats
    draws: Optional[DrawPipelineResult] = None
    two_odds: Optional[TwoOddsResult] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier_10": self.tier_10.to_dict(),
            "tier_20": self.tier_20.to_dict(),
            "total_screened": self.filter_stats.total,
            "draws": self.draws.to_dict() if self.draws else None,
            "two_odds": self.two_odds.to_dict() if self.two_odds else None,
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
        include_draws: bool = False,
        include_two_odds: bool = False,
        two_odds_cap: float = 2.0,
        two_odds_max_legs: int = 3,
        two_odds_min_legs: int = 2,
        two_odds_source: str = "form",
        two_odds_short_window: int = 5,
        two_odds_markets: Optional[list[str]] = None,
        two_odds_per_fixture: int = 3,
        top_rank_by_edge: bool = False,
        top_min_edge: float = 0.0,
        top_max_per_market: int = 0,
        top_pool_depth: int = 30,
    ) -> DualPipelineResult:
        """
        Generate BOTH Top 10 Bankers and Top 20 Mega Accumulator tickets simultaneously.

        ``include_draws`` adds the draw ladder as a third ticket set, built from
        the same fixtures and forms so it costs no extra SofaScore traffic.
        Defaults off: the draw track is unvalidated, so it stays opt-in.

        ``include_two_odds`` adds a fourth ticket: at most ``two_odds_max_legs``
        legs whose prices multiply to at most ``two_odds_cap``. It reuses the
        picks already screened above, so it costs no extra SofaScore traffic
        either — only the SportyBet prices needed to honour the cap.
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

        # Fetch form once for all matches. short_window builds the five-match
        # cut from the same events, for the form-guide ticket, at no extra cost.
        forms = await fetch_team_forms(
            fixtures, form_matches=form_matches, short_window=two_odds_short_window
        )

        # Screen up to 50 picks so Top 20 always has a full 20-match card
        all_screened = screen_fixtures(fixtures=fixtures, forms=forms, limit=50, max_per_fixture=1)
        if not all_screened:
            empty = PipelineResult(picks=[], filter_stats=stats, picks_text="")
            return DualPipelineResult(tier_10=empty, tier_20=empty, filter_stats=stats)

        # ── Price the shortlist ──────────────────────────────────────────
        #
        # Top 10/20 used to book without ever seeing a price: screened by
        # probability, sliced, booked. Three consequences followed from that
        # one omission. The ticket filled with a single market, because ranking
        # on probability alone always converges on the highest base rate.
        # Nothing compared the model's number against the number the price
        # implies, so an implausible edge could not announce itself. And
        # `--min-edge`, which PROJECT_STATE calls the quality control for this
        # engine, was never wired into the tickets actually being booked.
        #
        # Costs no SofaScore traffic — attach_odds uses the booker's SportyBet
        # service and caches markets per event.
        priced_pool = await self._price_shortlist(all_screened, depth=max(top_pool_depth, 20))

        # Defaults are deliberately inert: ranking and filtering on edge is
        # only as good as the probability feeding it, and this engine's Over
        # 1.5 rate has never been graded. Until it is, the price is *shown*
        # rather than acted on. Turn these on once there is a measured rate.
        ranked = self._rank_picks(
            priced_pool,
            rank_by_edge=top_rank_by_edge,
            min_edge=top_min_edge,
        )
        if not ranked:
            logger.warning(
                "Edge filter at %.3f removed every pick; falling back to the "
                "probability ranking rather than shipping nothing.", top_min_edge,
            )
            ranked = all_screened

        from core.predictor.tickets import cap_per_market

        # Top 10 Picks
        picks_10 = cap_per_market(ranked, top_max_per_market, limit=10)
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
        picks_20 = cap_per_market(ranked, top_max_per_market, limit=20)
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

        two_odds = None
        if include_two_odds:
            two_odds = await self.build_two_odds_ticket(
                all_screened,
                fixtures=fixtures,
                forms=forms,
                auto_book=auto_book,
                cap=two_odds_cap,
                max_legs=two_odds_max_legs,
                min_legs=two_odds_min_legs,
                source=two_odds_source,
                markets=two_odds_markets,
                per_fixture=two_odds_per_fixture,
            )

        draws = None
        if include_draws:
            # Reuses the fixtures and forms already fetched above. Screening
            # draws from a second fetch would double the SofaScore load for
            # identical data, and load is what got the local IP throttled.
            draws = await self.run_draw_pipeline(fixtures, forms, auto_book=auto_book)

        result = DualPipelineResult(
            tier_10=tier_10, tier_20=tier_20, filter_stats=stats,
            draws=draws, two_odds=two_odds,
        )

        # Record every leg with the price it was booked at. SportyBet keeps no
        # odds history, so a price not captured now is gone — which is why
        # "does Ticket 4 beat its price?" has never been answerable. Outcomes
        # can always be recovered from SofaScore later; the price cannot.
        # log_result swallows its own errors: a logging fault must never turn
        # into a failed digest.
        from core.ticket_log import log_result

        rows = log_result(result)
        if rows:
            logger.info("Ticket log: recorded %d legs with prices.", rows)

        return result

    async def _price_shortlist(self, picks: list[Pick], depth: int = 30) -> list["PricedPick"]:
        """
        Attach the live SportyBet price to the top ``depth`` screened picks.

        Failures are not fatal. A pick the bookmaker does not list comes back
        with ``odds`` unset and is still bookable through the normal path — the
        price is here to inform ranking and the digest, not to gate selection.
        """
        from core.predictor.odds import attach_odds

        try:
            return await attach_odds(picks[:depth], service=self.booker.service)
        except Exception as e:
            logger.warning("Could not price the shortlist (%s); continuing unpriced.", e)
            from core.predictor.odds import PricedPick
            return [PricedPick(pick=p, error=str(e)) for p in picks[:depth]]

    @staticmethod
    def _rank_picks(
        priced: list["PricedPick"],
        rank_by_edge: bool = False,
        min_edge: float = 0.0,
    ) -> list[Pick]:
        """
        Order the shortlist, optionally on edge against the live price.

        Default is the existing probability order, unchanged. Edge ranking is
        opt-in for a reason worth stating: edge is
        ``model probability - implied probability``, so it inherits every bias
        in the model probability. If the model is systematically ten points
        optimistic on a market, every pick in that market shows ten points of
        spurious edge and a min-edge filter will wave through exactly the picks
        it exists to catch. A filter is only as trustworthy as the number it
        filters on, and this engine's dominant market is ungraded.
        """
        kept = list(priced)

        if min_edge > 0:
            kept = [p for p in kept if p.edge is not None and p.edge >= min_edge]

        if rank_by_edge:
            # Unpriced picks sort last: no price means no measurable edge, and
            # guessing one would defeat the point of ranking on it.
            kept.sort(key=lambda p: (p.edge is not None, p.edge or 0.0), reverse=True)

        return [p.pick for p in kept]

    async def build_two_odds_ticket(
        self,
        screened: list[Pick],
        fixtures: Optional[list[Fixture]] = None,
        auto_book: bool = True,
        cap: float = 2.0,
        max_legs: int = 3,
        min_legs: int = 2,
        price_depth: int = 10,
        source: str = "form",
        forms: Optional[dict] = None,
        markets: Optional[list[str]] = None,
        per_fixture: int = 3,
    ) -> "TwoOddsResult":
        """
        Build the short banker: the best legs that multiply to at most ``cap``.

        ``source`` decides where the candidate picks come from:

        ``"form"``  reads both sides' last few results off the form guide and
                    takes the best of Home / Away / Draw / GG / Over 1.5 /
                    Over 2.5. This is the hand method the ticket was asked to
                    reproduce, and it costs no extra requests — the short
                    window is cut from the events already fetched for Top
                    10/20.
        ``"model"`` reuses the Poisson screen that feeds Top 10/20.

        The form source offers the builder ``per_fixture`` markets per fixture
        rather than one. Committing to a single market per fixture on
        probability alone is what left the builder unable to fill the cap: the
        safest market is not always the one that fits, and a fixture whose best
        read is priced too long had to be dropped whole. Extra selections on an
        already-matched fixture are close to free — ``attach_odds`` caches
        markets per event, so the SportyBet call count is per *fixture*, not
        per candidate.

        ``price_depth`` is therefore a fixture count for the form source, and a
        pick count for the model source.
        """
        from core.predictor.odds import attach_odds
        from core.predictor.tickets import build_capped_ticket

        result = TwoOddsResult(max_combined_odds=cap, source=source)

        candidates = screened
        if source == "form":
            if not fixtures or not forms:
                logger.warning("Form source requested without fixtures/forms; using the model screen.")
                result.source = "model (form inputs unavailable)"
            else:
                from core.predictor.form_pick import screen_form_candidates

                form_picks = screen_form_candidates(
                    fixtures,
                    forms,
                    prefer_short=True,
                    per_fixture=per_fixture,
                    fixture_limit=price_depth,
                    markets=markets,
                )
                if form_picks:
                    candidates = form_picks
                else:
                    # Every side too new to read. Say so and fall back rather
                    # than emit nothing.
                    logger.info("No fixture had enough form; falling back to the model screen.")
                    result.source = "model (form too thin)"

        if not candidates:
            return result

        # Reuses the booker's service, so the paginated event sweep it already
        # performed is not repeated. The form source has already capped itself
        # at ``price_depth`` FIXTURES, so it is not truncated again here — doing
        # so would cut a fixture's alternatives away and reintroduce the very
        # single-market-per-fixture limit this path exists to remove.
        to_price = candidates if result.source.startswith("form") else candidates[:price_depth]
        priced = await attach_odds(to_price, service=self.booker.service)
        ticket = build_capped_ticket(
            priced,
            max_combined_odds=cap,
            max_legs=max_legs,
            min_legs=min_legs,
            label=f"{cap:g} Odds Banker",
        )
        if not ticket:
            logger.info("No ticket could be built under a %.2f cap.", cap)
            return result

        result.ticket = ticket
        if auto_book:
            result.booking_result = await self.booker.book_predictions("\n".join(ticket.lines))

        return result

    async def run_draw_pipeline(
        self,
        fixtures: list[Fixture],
        forms: dict[int, Any],
        auto_book: bool = True,
        top_n: int = 10,
        shape: tuple[int, ...] = (10, 5, 5),
    ) -> DrawPipelineResult:
        """
        Screen draws and book the ladder, one code per ticket.

        Takes already-enriched fixtures and forms rather than fetching its own,
        so adding this to the daily digest costs no extra SofaScore traffic.

        ``shape`` defaults to the 10-fold plus the two five-folds it splits
        into. The five-folds are the ones with a realistic hit frequency —
        roughly 1.2 a year each against the 10-fold's once per 243 years — and
        they are disjoint, so a single bad leg cannot take out all three.

        Every pick is written to the draw ledger whether or not it is booked.
        Nothing about this track is validated yet: rho is unfitted and no draw
        pick has ever been graded, so treat the codes as a sample being built,
        not as a signal being acted on.
        """
        from core.predictor.draw_ledger import record_picks
        from core.predictor.draws import DrawScreenStats, screen_draws
        from core.predictor.odds import attach_odds
        from core.predictor.tickets import build_ladder

        screen_stats = DrawScreenStats()
        picks = screen_draws(fixtures, forms, limit=top_n, stats=screen_stats)
        logger.info(screen_stats.summary())

        if not picks:
            return DrawPipelineResult(
                picks=[], tickets=[], screen_summary=screen_stats.summary()
            )

        # Per-leg prices, needed for the ticket odds and for the ledger's edge
        # and closing-line columns. The booker's total_odds cannot supply these.
        priced = await attach_odds(picks, service=self.booker.service)
        record_picks(priced, ticket_label="daily-digest")

        # The 10-fold uses every pick; the five-folds split the same ten, so
        # overlap between tickets is intended here and disjointness is enforced
        # only within the pair.
        tickets: list[DrawTicket] = []
        ladder = build_ladder(priced, shape=(shape[0],), disjoint=False)
        ladder += build_ladder(priced, shape=tuple(shape[1:]), disjoint=True)

        for ticket in ladder:
            booking = None
            if auto_book:
                booking = await self.booker.book_predictions("\n".join(ticket.lines))
                if booking and booking.success:
                    logger.info(
                        "Draw %s booked: %s (odds %s)",
                        ticket.label,
                        booking.booking_code,
                        booking.total_odds,
                    )
                else:
                    logger.warning("Draw %s failed to book", ticket.label)
            tickets.append(DrawTicket(ticket=ticket, booking_result=booking))

        return DrawPipelineResult(
            picks=[p.pick for p in priced],
            tickets=tickets,
            screen_summary=screen_stats.summary(),
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

    @staticmethod
    def format_telegram_dual_digest(dual_res: DualPipelineResult, date_str: str) -> str:
        """Format both Top 10 Bankers and Top 20 Mega Accumulator tickets into a dual summary."""
        lines = [
            f"<b>🎯 Daily AI Predictions & Auto-Booked Tickets ({date_str})</b>\n",
            f"🧠 <i>Screened {dual_res.filter_stats.total} fixtures across 75+ elite competitions</i>\n",
            "━━━━━━━━━━━━━━━━━━━━",
            "🎯 <b>TICKET 1: TOP 10 BANKERS</b> (High Conviction)",
        ]

        if (
            dual_res.tier_10.booking_result
            and dual_res.tier_10.booking_result.success
            and dual_res.tier_10.booking_result.booking_code
        ):
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

        if (
            dual_res.tier_20.booking_result
            and dual_res.tier_20.booking_result.success
            and dual_res.tier_20.booking_result.booking_code
        ):
            b20 = dual_res.tier_20.booking_result
            lines.extend([
                f"🎟️ <b>SportyBet Code:</b> <code>{b20.booking_code}</code> <i>(tap to copy)</i>",
                f"📊 <b>Total Odds:</b> <b>{b20.total_odds or '—'}</b> | ⚽ <b>{len(dual_res.tier_20.picks)} Games</b>",
            ])
        lines.append("")

        for idx, p in enumerate(dual_res.tier_20.picks, 1):
            lines.append(f"<b>{idx}.</b> {p.fixture.home_name} vs {p.fixture.away_name} ➔ <b>{p.selection}</b> <i>({p.market})</i>")

        if dual_res.two_odds and dual_res.two_odds.ticket:
            lines.append(PredictionBookingPipeline.format_telegram_two_odds_section(dual_res.two_odds))

        if dual_res.draws and dual_res.draws.tickets:
            lines.append(PredictionBookingPipeline.format_telegram_draw_section(dual_res.draws))

        return "\n".join(lines)

    @staticmethod
    def format_telegram_two_odds_section(two_odds: "TwoOddsResult") -> str:
        """
        Format the capped banker as its own ticket block.

        Shows the combined odds and the joint probability together, because
        either one alone is misleading: 1.83 looks dull without the ~68% beside
        it, and 68% looks poor without the price it was bought at.
        """
        ticket = two_odds.ticket
        if not ticket:
            return ""

        odds = ticket.combined_odds
        header = (
            f"\n━━━━━━━━━━━━━━━━━━━━"
            f"\n💰 <b>TICKET 4: {two_odds.max_combined_odds:g} ODDS BANKER</b>"
            f" <i>({ticket.size} legs, capped)</i>"
        )
        lines = [header]

        # A ticket that could not be built to the shape asked for says so
        # rather than looking like the intended one. A single leg presented as
        # "TICKET 4" is how a risky one-game bet got read as a banker.
        if ticket.note:
            lines.append(f"⚠️ <i>{ticket.note}</i>")

        if two_odds.booking_result and two_odds.booking_result.success:
            b = two_odds.booking_result
            lines.append(
                f"🎟️ <b>SportyBet Code:</b> <code>{b.booking_code}</code> <i>(tap to copy)</i>"
            )

        lines.append(
            f"📊 <b>Total Odds:</b> <b>{odds:.2f}</b>" if odds else "📊 <b>Total Odds:</b> —"
        )
        lines.append(
            f"🎯 <b>Joint probability:</b> {ticket.combined_probability:.0%}"
            f" | ⚽ <b>{ticket.size} Games</b>"
        )
        if two_odds.source.startswith("form"):
            lines.append("📈 <i>Picked on both sides' recent form</i>")
        lines.append("")

        for idx, (leg, leg_odd) in enumerate(zip(ticket.legs, ticket.leg_odds), 1):
            price = f" @ {leg_odd:.2f}" if leg_odd else ""
            lines.append(
                f"<b>{idx}.</b> {leg.fixture.home_name} vs {leg.fixture.away_name}"
                f" ➔ <b>{leg.selection}</b> <i>({round(leg.probability * 100)}%{price})</i>"
            )
            # The record itself, not just the number derived from it. A 9/12 and
            # a 4/4 can shrink to the same probability and are not the same bet.
            if leg.rationale:
                lines.append(f"    <i>{leg.rationale}</i>")

        return "\n".join(lines)

    @staticmethod
    def format_telegram_draw_section(draws: DrawPipelineResult) -> str:
        """
        Format the draw ladder as a third ticket block.

        Labelled as unvalidated on purpose. These sit in the same message as
        picks running at 75-85% a leg, and a reader scanning codes has no way
        to tell that these run near 30% unless the message says so.
        """
        lines = [
            "\n━━━━━━━━━━━━━━━━━━━━",
            "🎲 <b>TICKET 3: DAILY DRAWS</b> <i>(experimental — unvalidated)</i>",
        ]

        for entry in draws.tickets:
            ticket = entry.ticket
            odds = ticket.combined_odds
            lines.append(
                f"\n<b>{ticket.label}</b> — {ticket.size} legs"
                + (f" @ <b>{odds:,.0f}x</b>" if odds else "")
            )
            if entry.booking_result and entry.booking_result.success:
                lines.append(
                    f"🎟️ <b>Code:</b> <code>{entry.booking_result.booking_code}</code> "
                    f"<i>(tap to copy)</i>"
                )
            else:
                lines.append("⚠️ <i>Not booked</i>")
            lines.append(
                f"📉 Hits ~<b>{ticket.expected_hits_per_year:.2f}×/year</b> "
                f"at one a day (1 in {ticket.one_in:,.0f})"
                if ticket.one_in
                else ""
            )

        # Per ticket, not the whole screened pool. Listing every pick under a
        # single ticket's code meant the reader could not tell which legs the
        # code actually held — a 5-fold header over seven selections.
        for entry in draws.tickets:
            lines.append(f"\n<b>📋 {entry.ticket.label} selections:</b>")
            for idx, leg in enumerate(entry.ticket.legs, 1):
                lines.append(
                    f"<b>{idx}.</b> {leg.fixture.home_name} vs {leg.fixture.away_name} "
                    f"➔ <b>Draw</b> <i>({round(leg.probability * 100)}%)</i>"
                )

        booked = {id(leg) for e in draws.tickets for leg in e.ticket.legs}
        spare = [p for p in draws.picks if id(p) not in booked]
        if spare:
            lines.append(
                f"\n<i>{len(spare)} further pick(s) screened but not on any ticket: "
                + ", ".join(p.fixture.label for p in spare)
                + "</i>"
            )

        lines.append(
            "\n⚠️ <i>Draws hit ~30%, not 80% — 7 losses in 10 is the expected "
            "outcome, not a bad run. Model is unfitted and ungraded; stake as a "
            "lottery ticket while the sample builds.</i>"
        )

        return "\n".join(line for line in lines if line != "")
