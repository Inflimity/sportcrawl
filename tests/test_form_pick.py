"""
Tests for form-guide screening: read both sides' last few games, pick a market.
"""

import pytest

from core.predictor.enrich import TeamForm
from core.predictor.filter import Fixture
from core.predictor.form_pick import (
    BASE_RATES, best_market, market_rates, screen_form,
)


def _form(tid, name, results, btts=0.0, over25=0.0, used=None):
    return TeamForm(
        team_id=tid, name=name, recent_results=results,
        btts_rate=btts, over25_rate=over25,
        matches_used=used if used is not None else len(results),
    )


def _fixture(mid=1, home_id=10, away_id=20):
    return Fixture(match_id=mid, tournament="T", category="C",
                   home_name="Home", away_name="Away",
                   home_id=home_id, away_id=away_id, start_utc="", start_local="")


def test_result_markets_use_both_form_lines():
    """A home win needs one side to win AND the other to lose."""
    home = _form(10, "Home", "WWWWW")          # win rate 1.0
    away = _form(20, "Away", "WWWWW")          # also winning everything
    rates = market_rates(home, away)
    # home win = (home wins 1.0 + away losses 0.0) / 2
    assert rates["1"][0] == pytest.approx(0.5)
    assert rates["2"][0] == pytest.approx(0.5)


def test_a_strong_side_against_a_poor_one_reads_as_a_home_win():
    home = _form(10, "Home", "WWWWW")
    away = _form(20, "Away", "LLLLL")
    sel, prob, raw, n = best_market(home, away)
    assert sel == "1"
    assert raw == pytest.approx(1.0)
    assert prob < raw, "a five-match read must be shrunk"


def test_goal_markets_average_the_two_sides():
    home = _form(10, "Home", "WWDLW", btts=1.0, over25=0.8)
    away = _form(20, "Away", "DDWLL", btts=0.6, over25=0.4)
    rates = market_rates(home, away)
    assert rates["GG"][0] == pytest.approx(0.8)
    assert rates["Over 2.5"][0] == pytest.approx(0.6)


def test_short_windows_are_shrunk_toward_the_base_rate():
    """5/5 BTTS is not 100%."""
    home = _form(10, "Home", "WWWWW", btts=1.0)
    away = _form(20, "Away", "WWWWW", btts=1.0)
    _, prob, raw, _ = best_market(home, away, markets=["GG"])
    assert raw == 1.0
    assert BASE_RATES["GG"] < prob < 1.0


def test_a_longer_window_beats_an_equal_short_one():
    short_h = _form(10, "H", "WWWWW", btts=1.0, used=5)
    short_a = _form(20, "A", "WWWWW", btts=1.0, used=5)
    long_h = _form(30, "H2", "WWWWWWWWWW", btts=1.0, used=10)
    long_a = _form(40, "A2", "WWWWWWWWWW", btts=1.0, used=10)
    _, p_short, _, _ = best_market(short_h, short_a, markets=["GG"])
    _, p_long, _, _ = best_market(long_h, long_a, markets=["GG"])
    assert p_long > p_short


def test_thin_form_is_skipped():
    fx = _fixture()
    forms = {10: _form(10, "Home", "WW", used=2), 20: _form(20, "Away", "WWWWW")}
    assert screen_form([fx], forms, prefer_short=False, min_matches=4) == []


def test_missing_form_is_skipped_not_guessed():
    fx = _fixture()
    assert screen_form([fx], {}, prefer_short=False) == []


def test_prefers_the_short_window_when_present():
    """The five-match cut is what the hand method reads."""
    long_h = _form(10, "Home", "LLLLLLLLLL", btts=0.0, used=10)
    long_h.short = _form(10, "Home", "WWWWW", btts=1.0, used=5)
    long_a = _form(20, "Away", "WWWWWWWWWW", btts=0.0, used=10)
    long_a.short = _form(20, "Away", "LLLLL", btts=1.0, used=5)

    picks = screen_form([_fixture()], {10: long_h, 20: long_a}, prefer_short=True)
    assert len(picks) == 1
    # On the short window the home side is winning and both sides see goals.
    assert picks[0].selection in {"GG", "1"}
    assert "last 5" in picks[0].rationale


def test_picks_are_ranked_and_one_per_fixture():
    forms = {
        10: _form(10, "A", "WWWWW", btts=1.0), 20: _form(20, "B", "LLLLL"),
        30: _form(30, "C", "WDLWD", btts=0.4), 40: _form(40, "D", "DWLDW"),
    }
    fixtures = [_fixture(1, 10, 20), _fixture(2, 30, 40)]
    picks = screen_form(fixtures, forms, prefer_short=False)
    assert len(picks) == 2
    assert picks[0].probability >= picks[1].probability
    assert len({p.fixture.match_id for p in picks}) == 2


def test_rationale_shows_the_form_lines():
    forms = {10: _form(10, "A", "WWWWW", btts=1.0), 20: _form(20, "B", "LLLLL")}
    pick = screen_form([_fixture()], forms, prefer_short=False)[0]
    assert "WWWWW" in pick.rationale and "LLLLL" in pick.rationale
    assert "raw" in pick.rationale and "shrink" in pick.rationale
