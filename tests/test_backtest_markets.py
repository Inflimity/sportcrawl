"""
Tests for the market grading harness.

The graders are the part that must not be wrong. A hit rate computed with a
grader that disagrees with how the market is actually booked is worse than no
number at all — it looks authoritative and is not.
"""

import pytest

from tools.backtest_markets import GRADERS, Tally, grade, report_rates


@pytest.mark.parametrize("selection,home,away,expected", [
    # Over 1.5 needs two goals, not three. Getting this confused with Over 2.5
    # is the exact mistake that started this whole thread.
    ("Over 1.5", 1, 0, False), ("Over 1.5", 1, 1, True), ("Over 1.5", 2, 0, True),
    ("Over 1.5", 0, 0, False), ("Over 1.5", 5, 4, True),
    ("Over 2.5", 1, 1, False), ("Over 2.5", 2, 1, True), ("Over 2.5", 0, 3, True),
    ("GG", 1, 1, True), ("GG", 2, 0, False), ("GG", 0, 0, False),
    ("NG", 2, 0, True), ("NG", 1, 1, False),
    ("1", 2, 1, True), ("1", 1, 1, False), ("1", 0, 1, False),
    ("2", 0, 1, True), ("2", 1, 1, False),
    ("X", 1, 1, True), ("X", 2, 1, False),
    # Double chance: the draw counts for BOTH sides.
    ("1X", 1, 1, True), ("1X", 2, 1, True), ("1X", 0, 1, False),
    ("X2", 1, 1, True), ("X2", 0, 1, True), ("X2", 2, 1, False),
    ("12", 1, 1, False), ("12", 2, 1, True),
    ("Draw", 0, 0, True), ("Draw", 1, 0, False),
])
def test_graders(selection, home, away, expected):
    assert grade(selection, home, away) is expected


def test_an_ungraded_market_returns_none_rather_than_guessing():
    """Silently scoring an unknown market as a loss would understate the rate."""
    assert grade("Correct Score 2-1", 2, 1) is None


def test_every_selection_the_screener_emits_has_a_grader():
    """A market bookable but ungradeable is a hole in every future backtest."""
    from core.predictor.form_pick import BASE_RATES
    from core.predictor.screen import PROBABILITY_FLOOR

    for selection in set(BASE_RATES) | set(PROBABILITY_FLOOR):
        assert selection in GRADERS, f"{selection!r} has no grader"


def test_tally_break_even_is_the_inverse_of_the_average_price():
    t = Tally()
    t.add(True, 1.25)
    t.add(False, 1.35)
    assert t.rate == 0.5
    assert t.break_even == pytest.approx(1 / 1.30)


def test_tally_break_even_is_none_without_prices():
    t = Tally()
    t.add(True)
    assert t.break_even is None


def _match(h, a, comp="Premier League", cat="England"):
    return {"home_score": h, "away_score": a, "tournament_name": comp,
            "category_name": cat}


def test_report_rates_counts_what_actually_happened():
    results = [_match(1, 1), _match(0, 0), _match(3, 1), _match(2, 0)]
    out = report_rates(results, markets=("Over 1.5", "GG"))
    # Over 1.5: 1-1, 3-1, 2-0 = 3 of 4.  GG: 1-1, 3-1 = 2 of 4.
    assert "75.0%" in out
    assert "50.0%" in out


def test_report_rates_survives_a_market_with_no_grader():
    out = report_rates([_match(1, 1)], markets=("Over 1.5", "Anytime Scorer"))
    assert "Over 1.5" in out
