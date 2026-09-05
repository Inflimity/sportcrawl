"""
Tests for the draw track: Dixon-Coles scoring, ticket construction, and the
guardrails that stop the empirical term inventing edge.
"""

import pytest

from core.market_mapper import resolve_market_selection
from core.prediction_parser import MarketCategory, parse_prediction_line
from core.predictor.draws import (
    DEFAULT_RHO,
    DRAW_EMPIRICAL_CAP,
    DrawScreenStats,
    dc_tau,
    draw_probability,
    draw_probability_independent,
    score_matrix,
    screen_draw,
    screen_draws,
)
from core.predictor.enrich import TeamForm
from core.predictor.filter import Fixture
from core.predictor.tickets import Ticket, build_ladder


def make_fixture(match_id=1, home="Alpha FC", away="Beta United"):
    return Fixture(
        match_id=match_id,
        tournament="Test League",
        category="Testland",
        home_name=home,
        away_name=away,
        home_id=match_id * 2,
        away_id=match_id * 2 + 1,
        start_utc="2026-08-26T18:00:00+00:00",
        start_local="2026-08-26 18:00",
    )


def make_form(team_id, name, gf, ga, results, matches=10):
    return TeamForm(
        team_id=team_id,
        name=name,
        matches_used=matches,
        gf_avg=gf,
        ga_avg=ga,
        btts_rate=0.5,
        over15_rate=0.7,
        over25_rate=0.45,
        scored_rate=0.7,
        clean_sheet_rate=0.3,
        recent_results=results,
    )


class TestDixonColes:
    def test_tau_only_touches_the_four_low_cells(self):
        for h, a in [(0, 0), (0, 1), (1, 0), (1, 1)]:
            assert dc_tau(h, a, 1.2, 1.1, DEFAULT_RHO) != 1.0
        for h, a in [(2, 2), (0, 2), (3, 1), (4, 4)]:
            assert dc_tau(h, a, 1.2, 1.1, DEFAULT_RHO) == 1.0

    def test_negative_rho_inflates_the_draw_cells(self):
        """0-0 and 1-1 up, 1-0 and 0-1 down — the observed direction in football."""
        assert dc_tau(0, 0, 1.2, 1.1, -0.13) > 1.0
        assert dc_tau(1, 1, 1.2, 1.1, -0.13) > 1.0
        assert dc_tau(1, 0, 1.2, 1.1, -0.13) < 1.0
        assert dc_tau(0, 1, 1.2, 1.1, -0.13) < 1.0

    def test_grid_is_a_probability_distribution(self):
        for lam_home, lam_away in [(1.25, 1.25), (2.5, 0.4), (3.9, 3.9), (0.2, 0.2)]:
            grid = score_matrix(lam_home, lam_away)
            assert sum(sum(row) for row in grid) == pytest.approx(1.0)
            assert min(min(row) for row in grid) >= 0.0

    def test_correction_lifts_draws_by_about_three_points(self):
        """The entire case for this track rests on this margin."""
        for lam in (1.05, 1.25, 1.40):
            lift = draw_probability(lam, lam) - draw_probability_independent(lam, lam)
            assert 0.025 < lift < 0.040

    def test_rho_of_zero_reduces_to_independent_poisson(self):
        assert draw_probability(1.3, 1.1, rho=0.0) == pytest.approx(
            draw_probability_independent(1.3, 1.1), abs=1e-9
        )

    def test_draws_peak_when_even_and_low_scoring(self):
        even_low = draw_probability(1.05, 1.05)
        even_high = draw_probability(1.95, 1.95)
        mismatch = draw_probability(2.20, 0.75)
        assert even_low > even_high > mismatch

    def test_extreme_lambdas_stay_valid(self):
        """LAMBDA_MAX is 4.0; tau must not drive any cell negative there."""
        grid = score_matrix(4.0, 4.0, rho=-0.24)
        assert min(min(row) for row in grid) >= 0.0
        assert sum(sum(row) for row in grid) == pytest.approx(1.0)

    def test_rho_is_clamped_into_the_valid_region(self):
        """An out-of-range rho must not produce negative probabilities."""
        grid = score_matrix(1.2, 1.1, rho=-5.0)
        assert min(min(row) for row in grid) >= 0.0
        assert sum(sum(row) for row in grid) == pytest.approx(1.0)


class TestEmpiricalCap:
    def test_noisy_form_cannot_inflate_probability(self):
        """
        Two teams drawing most of their recent matches is noise, not a 40% draw
        chance. Uncapped, the blend produced 40.1% on a model that said 33.5%,
        and the difference would have been booked as edge that does not exist.
        """
        fixture = make_fixture()
        home = make_form(2, "Alpha FC", 1.05, 1.05, "DDDDDDWLDD")
        away = make_form(3, "Beta United", 1.05, 1.05, "DDDDDDLWDD")
        pick = screen_draw(fixture, home, away)
        assert pick is not None
        model_p = draw_probability(1.05, 1.05)
        assert pick.probability <= model_p + DRAW_EMPIRICAL_CAP + 1e-9

    def test_cap_binds_symmetrically_downward(self):
        fixture = make_fixture()
        home = make_form(2, "Alpha FC", 1.05, 1.05, "WWWWWWWWWW")
        away = make_form(3, "Beta United", 1.05, 1.05, "LLLLLLLLLL")
        model_p = draw_probability(1.05, 1.05)
        pick = screen_draw(fixture, home, away)
        # Either rejected on conviction, or floored at model - cap. Never below.
        if pick is not None:
            assert pick.probability >= model_p - DRAW_EMPIRICAL_CAP - 1e-9

    def test_probability_never_exceeds_the_structural_ceiling(self):
        """No blend of form should ever price a draw above ~37%."""
        fixture = make_fixture()
        home = make_form(2, "Alpha FC", 0.9, 0.9, "DDDDDDDDDD")
        away = make_form(3, "Beta United", 0.9, 0.9, "DDDDDDDDDD")
        pick = screen_draw(fixture, home, away)
        assert pick is not None
        assert pick.probability < 0.40


class TestScreening:
    def test_selects_tight_rejects_mismatch(self):
        cases = [
            ("Tight", (1.05, 1.05, "DDWLD"), (1.05, 1.05, "DLDWD")),
            ("Open", (1.95, 1.85, "WWLWL"), (1.90, 1.90, "WLWWL")),
            ("Mismatch", (2.40, 0.70, "WWWWD"), (0.70, 2.30, "LLLDL")),
        ]
        fixtures, forms = [], {}
        for i, (label, (hgf, hga, hr), (agf, aga, ar)) in enumerate(cases):
            fixtures.append(make_fixture(match_id=i, home=f"{label} H", away=f"{label} A"))
            forms[i * 2] = make_form(i * 2, f"{label} H", hgf, hga, hr)
            forms[i * 2 + 1] = make_form(i * 2 + 1, f"{label} A", agf, aga, ar)

        picks = screen_draws(fixtures, forms)
        selected = {p.fixture.home_name for p in picks}
        assert "Tight H" in selected
        assert "Mismatch H" not in selected

    def test_thin_form_is_skipped(self):
        fixture = make_fixture()
        home = make_form(2, "Alpha FC", 1.05, 1.05, "D", matches=1)
        away = make_form(3, "Beta United", 1.05, 1.05, "D", matches=1)
        assert screen_draw(fixture, home, away) is None

    def test_picks_are_ranked_by_conviction(self):
        fixtures, forms = [], {}
        for i, (gf, ga) in enumerate([(1.05, 1.05), (1.20, 1.15), (1.35, 1.30)]):
            fixtures.append(make_fixture(match_id=i, home=f"H{i}", away=f"A{i}"))
            forms[i * 2] = make_form(i * 2, f"H{i}", gf, ga, "DDWLD")
            forms[i * 2 + 1] = make_form(i * 2 + 1, f"A{i}", ga, gf, "DLDWD")
        picks = screen_draws(fixtures, forms)
        convictions = [p.conviction for p in picks]
        assert convictions == sorted(convictions, reverse=True)

    def test_limit_truncates(self):
        fixtures, forms = [], {}
        for i in range(5):
            fixtures.append(make_fixture(match_id=i, home=f"H{i}", away=f"A{i}"))
            forms[i * 2] = make_form(i * 2, f"H{i}", 1.05, 1.05, "DDWLD")
            forms[i * 2 + 1] = make_form(i * 2 + 1, f"A{i}", 1.05, 1.05, "DLDWD")
        assert len(screen_draws(fixtures, forms, limit=3)) == 3

    def test_stats_account_for_every_fixture(self):
        fixtures = [make_fixture(match_id=i) for i in range(4)]
        forms = {}
        for i in range(4):
            forms[i * 2] = make_form(i * 2, "H", 1.8 + i * 0.3, 1.5, "WWLWL")
            forms[i * 2 + 1] = make_form(i * 2 + 1, "A", 0.7, 2.1, "LLWLL")
        stats = DrawScreenStats()
        screen_draws(fixtures, forms, stats=stats)
        accounted = (
            stats.no_form
            + stats.unreliable_form
            + stats.below_probability_floor
            + stats.below_conviction_floor
            + stats.passed
        )
        assert accounted == stats.considered == 4

    def test_missing_form_is_counted_not_crashed(self):
        fixtures = [make_fixture(match_id=0)]
        stats = DrawScreenStats()
        assert screen_draws(fixtures, {}, stats=stats) == []
        assert stats.no_form == 1


class TestOutputContract:
    def test_draw_line_round_trips_through_the_parser(self):
        fixture = make_fixture()
        home = make_form(2, "Alpha FC", 1.05, 1.05, "DDWLD")
        away = make_form(3, "Beta United", 1.05, 1.05, "DLDWD")
        pick = screen_draw(fixture, home, away)
        assert pick is not None

        bet = parse_prediction_line(pick.line)
        assert bet is not None
        assert bet.home_team == "Alpha FC"
        assert bet.away_team == "Beta United"
        assert bet.market_category == MarketCategory.MATCH_WINNER
        assert bet.selection == "X"

    def test_draw_resolves_against_a_sportybet_market(self):
        """The booker must be able to find the draw outcome on a real payload."""
        bet = parse_prediction_line("Alpha FC vs Beta United - Draw")
        markets = [
            {
                "id": "1",
                "desc": "1X2",
                "outcomes": [
                    {"id": "1", "desc": "Home", "odds": "2.10"},
                    {"id": "2", "desc": "Draw", "odds": "3.30"},
                    {"id": "3", "desc": "Away", "odds": "3.50"},
                ],
            }
        ]
        resolved = resolve_market_selection(bet, markets)
        assert resolved is not None
        assert resolved["odds"] == "3.30"


# Realistic names on purpose. "Home 1 vs Away 1" parses wrong: the bare digits
# collide with the parser's own 1/X/2 selection tokens, so the test data would
# be exercising a naming quirk rather than the ticket logic.
TEAM_NAMES = [
    ("Getafe", "Osasuna"),
    ("Cagliari", "Empoli"),
    ("Reims", "Nantes"),
    ("Union Berlin", "Mainz"),
    ("Rio Ave", "Boavista"),
    ("Hearts", "St Mirren"),
    ("Lecce", "Verona"),
    ("Metz", "Le Havre"),
    ("Cadiz", "Leganes"),
    ("Sturm Graz", "LASK"),
]


class FakePick:
    """A minimal stand-in for Pick, enough for ticket arithmetic."""

    def __init__(self, index, probability):
        home, away = TEAM_NAMES[index % len(TEAM_NAMES)]
        self.fixture = make_fixture(match_id=index, home=home, away=away)
        self.probability = probability
        self.conviction = probability
        self.selection = "Draw"
        self.line = f"{home} vs {away} - Draw"


class FakePriced:
    """Stands in for PricedPick without needing the network."""

    def __init__(self, pick, odds):
        self.pick = pick
        self.odds = odds


def pool(count=10, prob=0.32, odds=3.20):
    return [FakePriced(FakePick(i, prob), odds) for i in range(count)]


class TestTickets:
    def test_five_fold_lands_about_once_a_year(self):
        ticket = build_ladder(pool(), shape=(5,))[0]
        assert ticket.combined_probability == pytest.approx(0.32**5)
        assert ticket.expected_hits_per_year == pytest.approx(1.22, abs=0.02)

    def test_ten_fold_is_effectively_unreachable(self):
        ticket = build_ladder(pool(), shape=(10,))[0]
        assert ticket.one_in > 80_000
        assert ticket.expected_hits_per_year < 0.01

    def test_edge_compounds_with_leg_count(self):
        """Positive per-leg edge multiplies — the honest upside of an accumulator."""
        five = build_ladder(pool(), shape=(5,))[0]
        ten = build_ladder(pool(), shape=(10,))[0]
        assert five.expected_value > 1.0
        assert ten.expected_value > five.expected_value

    def test_negative_per_leg_edge_also_compounds(self):
        """The same arithmetic in the other direction, which is the real risk."""
        ticket = build_ladder(pool(prob=0.28, odds=3.20), shape=(10,))[0]
        assert ticket.expected_value < 1.0

    def test_default_ladder_is_disjoint(self):
        tickets = build_ladder(pool(), shape=(5, 5))
        assert len(tickets) == 2
        first = {leg.line for leg in tickets[0].legs}
        second = {leg.line for leg in tickets[1].legs}
        assert first.isdisjoint(second)

    def test_overlapping_ladder_shares_legs(self):
        tickets = build_ladder(pool(), shape=(5, 5), disjoint=False)
        assert {leg.line for leg in tickets[0].legs} == {leg.line for leg in tickets[1].legs}

    def test_underfilled_ticket_is_skipped_not_shortened(self):
        """A 4-fold emitted where 5 was asked for changes the payout threefold."""
        tickets = build_ladder(pool(count=7), shape=(5, 5))
        assert len(tickets) == 1
        assert tickets[0].size == 5

    def test_unpriced_picks_are_excluded_from_the_pool(self):
        mixed = pool(count=5) + [FakePriced(FakePick(99, 0.32), None)]
        tickets = build_ladder(mixed, shape=(5,))
        assert len(tickets) == 1
        assert all(o is not None for o in tickets[0].leg_odds)

    def test_empty_pool_builds_nothing(self):
        assert build_ladder([], shape=(5,)) == []

    def test_unpriced_leg_leaves_odds_unknown(self):
        ticket = Ticket(legs=[FakePick(0, 0.32)] * 3, leg_odds=[3.2, None, 3.1])
        assert ticket.combined_odds is None
        assert ticket.expected_value is None

    def test_payout_cap_limits_useful_stake(self):
        """At a 10m cap a 10-fold's stake stops earning below 100 units."""
        ten = build_ladder(pool(), shape=(10,))[0]
        five = build_ladder(pool(), shape=(5,))[0]
        assert ten.max_useful_stake(10_000_000) < 100
        assert five.max_useful_stake(10_000_000) > 25_000
        assert ten.max_useful_stake(None) is None

    def test_lines_are_booker_ready(self):
        ticket = build_ladder(pool(), shape=(5,))[0]
        assert len(ticket.lines) == 5
        for line in ticket.lines:
            bet = parse_prediction_line(line)
            assert bet is not None
            assert bet.selection == "X"


class TestPipelineIntegration:
    """The daily digest path: three codes, and no extra SofaScore traffic."""

    def _fixtures_and_forms(self, count=12):
        fixtures, forms = {}, {}
        fixture_list = []
        for i in range(count):
            home, away = TEAM_NAMES[i % len(TEAM_NAMES)]
            home, away = f"{home}", f"{away} {i}" if i >= len(TEAM_NAMES) else away
            fixture_list.append(make_fixture(match_id=i, home=home, away=away))
            forms[i * 2] = make_form(i * 2, home, 1.05, 1.05, "DDWLD")
            forms[i * 2 + 1] = make_form(i * 2 + 1, away, 1.05, 1.05, "DLDWD")
        return fixture_list, forms

    def _run(self, auto_book=True, shape=(10, 5, 5)):
        import asyncio
        from unittest.mock import patch

        from core.predictor.odds import PricedPick
        from services.pipeline import PredictionBookingPipeline
        from services.sportybet_service import BookingResult

        fixtures, forms = self._fixtures_and_forms()
        pipeline = PredictionBookingPipeline.__new__(PredictionBookingPipeline)
        pipeline.booker = type("B", (), {"service": None})()

        booked = []

        async def fake_book(text, kickoffs=None):
            booked.append(text)
            # Legs now travel with their kickoffs so the booker cannot
            # match a same-day event that merely shares a club name.
            assert kickoffs, 'draw legs must be booked with their kickoffs'
            return BookingResult(
                success=True, booking_code=f"CODE{len(booked)}", total_odds="336.00"
            )

        pipeline.booker.book_predictions = fake_book

        async def fake_odds(picks, service=None, country_code="ng"):
            return [PricedPick(pick=p, odds=3.20) for p in picks]

        with patch("core.predictor.odds.attach_odds", fake_odds), patch(
            "core.predictor.draw_ledger.record_picks", lambda *a, **k: 0
        ):
            result = asyncio.run(
                pipeline.run_draw_pipeline(fixtures, forms, auto_book=auto_book, shape=shape)
            )
        return result, booked

    def test_produces_three_tickets_with_three_codes(self):
        result, booked = self._run()
        assert len(result.tickets) == 3
        assert [t.ticket.size for t in result.tickets] == [10, 5, 5]
        assert len(booked) == 3
        codes = [t.booking_result.booking_code for t in result.tickets]
        assert len(set(codes)) == 3

    def test_ten_fold_uses_all_picks_and_fives_are_disjoint(self):
        result, _ = self._run()
        ten, five_a, five_b = (t.ticket for t in result.tickets)
        assert len(ten.legs) == 10
        assert {leg.line for leg in five_a.legs}.isdisjoint({leg.line for leg in five_b.legs})
        # The five-folds are drawn from the same ten the 10-fold uses.
        assert {leg.line for leg in five_a.legs} <= {leg.line for leg in ten.legs}

    def test_auto_book_off_still_screens_and_prices(self):
        result, booked = self._run(auto_book=False)
        assert booked == []
        assert len(result.tickets) == 3
        assert all(t.booking_result is None for t in result.tickets)
        assert all(t.ticket.combined_odds for t in result.tickets)

    def test_no_candidates_returns_empty_not_error(self):
        import asyncio

        from services.pipeline import PredictionBookingPipeline

        pipeline = PredictionBookingPipeline.__new__(PredictionBookingPipeline)
        pipeline.booker = type("B", (), {"service": None})()
        # Heavy mismatches: nothing should clear the draw floors.
        fixtures, forms = [], {}
        for i in range(4):
            fixtures.append(make_fixture(match_id=i))
            forms[i * 2] = make_form(i * 2, "H", 2.6, 0.6, "WWWWW")
            forms[i * 2 + 1] = make_form(i * 2 + 1, "A", 0.6, 2.6, "LLLLL")
        result = asyncio.run(pipeline.run_draw_pipeline(fixtures, forms, auto_book=True))
        assert result.picks == []
        assert result.tickets == []
        assert "0 candidates" in result.screen_summary

    def test_digest_section_labels_the_track_unvalidated(self):
        from services.pipeline import PredictionBookingPipeline

        result, _ = self._run()
        text = PredictionBookingPipeline.format_telegram_draw_section(result)
        assert "unvalidated" in text
        # A reader scanning codes must not mistake 30% legs for 80% legs.
        assert "30%" in text
        for ticket in result.tickets:
            assert ticket.booking_result.booking_code in text

    def test_result_serialises_for_the_web_ui(self):
        result, _ = self._run()
        payload = result.to_dict()
        assert payload["picks_count"] == 10
        assert len(payload["tickets"]) == 3
        assert payload["tickets"][0]["legs"] == 10
        assert payload["tickets"][0]["booking"]["booking_code"] == "CODE1"


class TestClosingSweep:
    """Closing line value is the only signal that reads before results exist."""

    def _ledger(self, tmp_path, rows):
        import json

        path = tmp_path / "draw_ledger.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        return path

    def _row(self, line, offset, event_id="sr:match:1", odds=3.40, closing=None):
        import time

        return {
            "line": line,
            "kickoff_ts": int(time.time()) + offset,
            "event_id": event_id,
            "odds": odds,
            "closing_odds": closing,
            "result": None,
        }

    def test_captures_only_imminent_kickoffs(self, tmp_path):
        from core.predictor.draw_ledger import pending_closing_capture

        path = self._ledger(
            tmp_path,
            [
                self._row("Getafe vs Osasuna - Draw", 600),
                self._row("Reims vs Nantes - Draw", 7200),  # too far out
            ],
        )
        pending = pending_closing_capture(within_seconds=900, path=path)
        assert [r["line"] for r in pending] == ["Getafe vs Osasuna - Draw"]

    def test_skips_kickoffs_already_past(self, tmp_path):
        """A price after the whistle is not a closing price."""
        from core.predictor.draw_ledger import pending_closing_capture

        path = self._ledger(tmp_path, [self._row("Metz vs Le Havre - Draw", -600)])
        assert pending_closing_capture(within_seconds=900, path=path) == []

    def test_skips_rows_already_captured(self, tmp_path):
        from core.predictor.draw_ledger import pending_closing_capture

        path = self._ledger(
            tmp_path, [self._row("Lecce vs Verona - Draw", 400, closing=3.30)]
        )
        assert pending_closing_capture(within_seconds=900, path=path) == []

    def test_skips_rows_without_an_event_id(self, tmp_path):
        """Without the id a capture would cost a full paginated re-scan."""
        from core.predictor.draw_ledger import pending_closing_capture

        path = self._ledger(
            tmp_path, [self._row("Hearts vs St Mirren - Draw", 500, event_id=None)]
        )
        assert pending_closing_capture(within_seconds=900, path=path) == []

    def test_write_back_preserves_every_row(self, tmp_path):
        from core.predictor.draw_ledger import (
            apply_closing_odds,
            load_ledger,
            pending_closing_capture,
            row_key,
        )

        path = self._ledger(
            tmp_path,
            [
                self._row("Getafe vs Osasuna - Draw", 600, "sr:match:1", 3.40),
                self._row("Cagliari vs Empoli - Draw", 300, "sr:match:2", 3.20),
                self._row("Reims vs Nantes - Draw", 7200, "sr:match:3", 3.10),
            ],
        )
        pending = pending_closing_capture(within_seconds=900, path=path)
        captured = {row_key(r): 3.15 for r in pending}
        assert apply_closing_odds(captured, path=path) == 2

        rows = load_ledger(path)
        assert len(rows) == 3
        assert rows[2]["closing_odds"] is None  # untouched
        assert all(r["closing_odds"] == 3.15 for r in rows[:2])

    def test_write_back_is_idempotent(self, tmp_path):
        from core.predictor.draw_ledger import apply_closing_odds, load_ledger, row_key

        path = self._ledger(tmp_path, [self._row("Getafe vs Osasuna - Draw", 600)])
        key = row_key(load_ledger(path)[0])
        assert apply_closing_odds({key: 3.15}, path=path) == 1
        # A stored closing price is never overwritten by a later pass.
        assert apply_closing_odds({key: 9.99}, path=path) == 0
        assert load_ledger(path)[0]["closing_odds"] == 3.15

    def test_row_key_separates_repeat_fixtures(self):
        from core.predictor.draw_ledger import row_key

        a = {"line": "Getafe vs Osasuna - Draw", "kickoff_ts": 1000}
        b = {"line": "Getafe vs Osasuna - Draw", "kickoff_ts": 2000}
        assert row_key(a) != row_key(b)

    def test_clv_reads_before_anything_is_graded(self, tmp_path):
        """The whole point: readable at ~150 picks, weeks before hit rate is."""
        from core.predictor.draw_ledger import summarise

        rows = []
        for i in range(10):
            row = self._row(f"Team {i} vs Rival {i} - Draw", 600, odds=3.40)
            row["closing_odds"] = 3.20 if i < 7 else 3.60  # beat the close 7/10
            rows.append(row)
        path = self._ledger(tmp_path, rows)

        text = summarise(path)
        assert "graded: 0" in text
        assert "beat the close on 7/10" in text
        assert "140 more captures" in text

    def test_summary_warns_when_nothing_captured(self, tmp_path):
        from core.predictor.draw_ledger import summarise

        path = self._ledger(tmp_path, [self._row("Getafe vs Osasuna - Draw", 600)])
        assert "nothing captured yet" in summarise(path)

    def test_sweep_resolves_the_draw_price_and_skips_suspended(self, tmp_path):
        import asyncio
        from unittest.mock import patch

        import core.predictor.draw_ledger as ledger
        from tools.closing_sweep import capture_once

        path = self._ledger(
            tmp_path,
            [
                self._row("Getafe vs Osasuna - Draw", 600, "sr:match:1", 3.40),
                self._row("Reims vs Nantes - Draw", 400, "sr:match:3", 3.10),
            ],
        )
        markets = {
            "sr:match:1": [
                {
                    "id": "1",
                    "desc": "1X2",
                    "outcomes": [
                        {"id": "1", "desc": "Home", "odds": "2.10"},
                        {"id": "2", "desc": "Draw", "odds": "3.15"},
                        {"id": "3", "desc": "Away", "odds": "3.90"},
                    ],
                }
            ],
            "sr:match:3": [],  # suspended
        }

        class FakeService:
            calls = []

            async def fetch_event_markets(self, event_id):
                FakeService.calls.append(event_id)
                return markets.get(event_id, [])

        with patch(
            "tools.closing_sweep.pending_closing_capture",
            lambda **kw: ledger.pending_closing_capture(path=path, **kw),
        ), patch(
            "tools.closing_sweep.apply_closing_odds",
            lambda c: ledger.apply_closing_odds(c, path=path),
        ):
            updated = asyncio.run(capture_once(FakeService(), window_seconds=900))

        assert updated == 1
        rows = ledger.load_ledger(path)
        assert rows[0]["closing_odds"] == 3.15
        assert rows[0]["closing_captured_at"] is not None
        # Suspended market left alone so a later pass can retry it.
        assert rows[1]["closing_odds"] is None

    def test_sweep_caches_markets_per_event(self, tmp_path):
        """Two picks on one fixture must not cost two requests."""
        import asyncio
        from unittest.mock import patch

        import core.predictor.draw_ledger as ledger
        from tools.closing_sweep import capture_once

        path = self._ledger(
            tmp_path,
            [
                self._row("Getafe vs Osasuna - Draw", 600, "sr:match:1", 3.40),
                self._row("Getafe vs Osasuna - Draw", 500, "sr:match:1", 3.30),
            ],
        )
        payload = [
            {
                "id": "1",
                "desc": "1X2",
                "outcomes": [
                    {"id": "1", "desc": "Home", "odds": "2.10"},
                    {"id": "2", "desc": "Draw", "odds": "3.15"},
                    {"id": "3", "desc": "Away", "odds": "3.90"},
                ],
            }
        ]
        calls = []

        class FakeService:
            async def fetch_event_markets(self, event_id):
                calls.append(event_id)
                return payload

        with patch(
            "tools.closing_sweep.pending_closing_capture",
            lambda **kw: ledger.pending_closing_capture(path=path, **kw),
        ), patch(
            "tools.closing_sweep.apply_closing_odds",
            lambda c: ledger.apply_closing_odds(c, path=path),
        ):
            asyncio.run(capture_once(FakeService(), window_seconds=900))

        assert len(calls) == 1


class TestPricedPickCarriesEventId:
    def test_event_id_field_exists_and_defaults_none(self):
        from core.predictor.odds import PricedPick

        assert PricedPick(pick=FakePick(0, 0.32)).event_id is None
        assert PricedPick(pick=FakePick(0, 0.32), event_id="sr:match:9").event_id == "sr:match:9"
