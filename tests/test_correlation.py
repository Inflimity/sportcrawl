"""
Guards for the correlation table and the builder sheet.

These tests exist to stop the two failure modes that would make this module
worse than useless: reporting a correlation computed from too few matches, and
silently approximating a joint probability we cannot actually measure.
"""

from __future__ import annotations

from core.predictor.correlation import (
    MARKETS,
    MIN_JOINT_SAMPLE,
    CorrelationTable,
    build_table,
)
from core.predictor.builder_sheet import build_fixture_sheet, format_sheet


def _rows(spec: list[tuple[int, int, int, int]], times: int = 1):
    """Repeat a small set of scorelines to reach a usable sample."""
    return [r for _ in range(times) for r in spec]


def test_markets_are_mutually_consistent():
    """A scoreline must not satisfy contradictory selections."""
    for h, a, hh, ah in [(0, 0, 0, 0), (2, 1, 1, 0), (1, 1, 0, 1), (0, 3, 0, 2), (4, 4, 2, 2)]:
        m = {k: fn(h, a, hh, ah) for k, fn in MARKETS.items()}
        assert sum([m["HOME"], m["DRAW"], m["AWAY"]]) == 1
        assert sum([m["HT_HOME"], m["HT_DRAW"], m["HT_AWAY"]]) == 1
        assert m["BTTS"] != m["NG"]
        assert not (m["O2.5"] and m["U2.5"])
        # A full-match over implies every lower line.
        if m["O2.5"]:
            assert m["O1.5"] and m["O0.5"]


def test_half_time_above_full_time_is_dropped():
    """A HT score above the FT score means one is wrong; trust neither."""
    good = _rows([(2, 1, 1, 0)], times=300)
    bad = [(1, 0, 2, 0), (0, 1, 0, 3)]      # impossible
    table = build_table(good + bad)
    assert table.n == 300


def test_joint_below_min_sample_returns_none():
    """
    A pair seen fewer than MIN_JOINT_SAMPLE times must not be reported.

    This is ANALYSIS.md §2 in test form: a rate computed from a handful of
    matches is noise, and noise presented as a probability is how a losing bet
    gets a confident number attached to it.
    """
    rows = _rows([(2, 1, 1, 0)], times=200) + _rows([(0, 0, 0, 0)], times=5)
    table = build_table(rows)
    # HOME+HT_HOME is in every one of the 200, so it is reportable.
    assert table.joint_p("HOME", "HT_HOME") is not None
    # NG+DRAW occurs only in the 5 goalless draws — under the threshold.
    assert table.joint.get(("DRAW", "NG"), 0) < MIN_JOINT_SAMPLE
    assert table.joint_p("DRAW", "NG") is None


def test_ratio_detects_positive_and_negative_correlation():
    rows = (
        _rows([(2, 1, 1, 0)], times=100)    # home win, HT home, O2.5, BTTS
        + _rows([(0, 0, 0, 0)], times=100)  # goalless draw
        + _rows([(1, 1, 0, 0)], times=100)  # draw, BTTS, U2.5
    )
    table = build_table(rows)
    # HOME and HT_HOME always co-occur here -> strongly positive.
    assert (table.ratio("HOME", "HT_HOME") or 0) > 1.5
    # DRAW never co-occurs with O2.5 in this sample.
    assert table.is_impossible("DRAW", "O2.5")


def test_three_leg_combination_is_not_approximated():
    """
    Three or more legs must come back unpriced rather than chained from pairs.

    Chaining pairwise ratios assumes the interactions are independent of one
    another, which for football markets is exactly the assumption that fails.
    A number we cannot stand behind is worse than no number here.
    """
    table = build_table(_rows([(2, 1, 1, 0), (0, 0, 0, 0), (1, 1, 0, 0)], times=100))
    sheet = build_fixture_sheet(
        "A", "B",
        {"HOME": 0.5, "O1.5": 0.7, "BTTS": 0.6},
        table,
        include_unpriceable=False,
        max_combination_legs=3,
    )
    assert all(len(c.markets) == 2 for c in sheet.combinations)


def test_required_odds_exceed_fair_odds():
    """The margin must always push the required price above the fair price."""
    table = build_table(_rows([(2, 1, 1, 0), (0, 0, 0, 0), (1, 1, 0, 0)], times=100))
    sheet = build_fixture_sheet("A", "B", {"HOME": 0.5, "HT_HOME": 0.35}, table,
                                include_unpriceable=False)
    for c in sheet.combinations:
        if c.fair_odds:
            assert c.required_odds(0.20) > c.fair_odds


def test_empty_table_prices_nothing():
    """With no history, every combination is unpriceable — not 'even money'."""
    sheet = build_fixture_sheet("A", "B", {"HOME": 0.5, "O1.5": 0.7},
                                CorrelationTable(), include_unpriceable=False)
    assert all(c.true_probability is None for c in sheet.combinations)
    assert all(c.ratio is None for c in sheet.combinations)


def test_corner_selections_are_listed_but_unpriced():
    """
    Corners must appear as bookable and carry no probability.

    ANALYSIS.md §8 found no repeatable corner edge, and no corner history is
    collected yet. Showing them with a number would be inventing one.
    """
    table = build_table(_rows([(2, 1, 1, 0)], times=200))
    sheet = build_fixture_sheet("A", "B", {"HOME": 0.5}, table, include_unpriceable=True)
    corners = [s for s in sheet.selections if "CORNER" in s.market]
    assert corners, "corner selections should be listed"
    assert all(s.probability is None for s in corners)
    assert all("no validated history" in s.note for s in corners)
    # And they must never reach the combination pricing.
    assert not any("CORNER" in m for c in sheet.combinations for m in c.markets)


def test_format_sheet_states_the_margin():
    table = build_table(_rows([(2, 1, 1, 0), (1, 1, 0, 0)], times=150))
    sheet = build_fixture_sheet("A", "B", {"HOME": 0.5, "HT_HOME": 0.35, "BTTS": 0.55},
                                table, include_unpriceable=False)
    text = format_sheet([sheet])
    assert "need better than" in text
    assert "margin" in text.lower()
