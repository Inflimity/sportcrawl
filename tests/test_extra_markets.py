"""
Tests for the corner / shots / team-total legs and the mixed Top 20 card.

The important assertions are the refusals. These markets have no graded rate,
so the value of this code is entirely in what it declines to emit: a leg from a
thin sample, a line chosen by maximising over noise, or two legs on one match.
"""

from __future__ import annotations

import pytest

from core.predictor.enrich import TeamForm
from core.predictor.extra_markets import (
    CORNER_LINE_RANGE,
    _half_line,
    screen_corners,
    screen_extra_markets,
    screen_shots,
    screen_team_goals,
)
from core.predictor.filter import Fixture
from core.predictor.screen import Pick
from core.predictor.stats_form import (
    MIN_STAT_MATCHES,
    StatForm,
    build_stat_forms,
    corner_over_probability,
    expected_corners,
)
from core.predictor.tickets import build_mixed_card


def fx(i=1):
    return Fixture(
        match_id=i, tournament="Premier League", category="England",
        home_name=f"Home{i}", away_name=f"Away{i}",
        home_id=i * 10, away_id=i * 10 + 1,
        start_utc="2026-09-19 18:00:00", start_local="19:00",
    )


def stat(team_id, matches=10, cf=6.0, ca=4.0, sf=5.0, sa=4.0):
    return StatForm(
        team_id=team_id, corner_matches=matches, corners_for=cf, corners_against=ca,
        sot_matches=matches, sot_for=sf, sot_against=sa,
    )


def team_form(team_id, gf=1.8, ga=1.0, matches=10):
    return TeamForm(
        team_id=team_id, name=f"T{team_id}", matches_used=matches,
        gf_avg=gf, ga_avg=ga, over15_rate=0.8, over25_rate=0.6,
        btts_rate=0.6, scored_rate=0.9, recent_results="WWDWL",
    )


# ── Sample floors: the refusals ──────────────────────────────────────────


def test_no_corner_leg_below_the_sample_floor():
    thin = stat(10, matches=MIN_STAT_MATCHES - 1)
    ok = stat(11, matches=MIN_STAT_MATCHES)
    assert expected_corners(thin, ok) is None
    assert expected_corners(ok, thin) is None
    assert expected_corners(ok, ok) is not None


def test_a_team_with_no_statistics_emits_nothing():
    assert screen_corners(fx(), StatForm(team_id=10), StatForm(team_id=11)) is None
    assert screen_shots(fx(), StatForm(team_id=10), StatForm(team_id=11)) is None


def test_extra_markets_skips_corners_when_stats_are_absent_but_still_gives_team_goals():
    picks = screen_extra_markets(fx(), team_form(10, gf=2.4), team_form(11, gf=2.0), {})
    assert picks, "team totals need no statistics and should survive"
    assert all("Corners" not in p.selection for p in picks)
    assert all(not p.validated for p in picks)


# ── The line is chosen before the probability, not by maximising it ──────


def test_line_is_a_fixed_function_of_expectation():
    """The same expected count must always give the same line, whatever the
    probabilities of neighbouring lines happen to be. This is the ANALYSIS.md
    §2 guard: no maximisation over a set of noisy candidates."""
    a, b = stat(10, cf=6.0, ca=5.0), stat(11, cf=5.0, ca=4.0)
    first = screen_corners(fx(), a, b)
    second = screen_corners(fx(), a, b)
    assert first is not None
    assert first.selection == second.selection


def test_line_sits_below_expectation():
    a, b = stat(10, cf=7.0, ca=6.0), stat(11, cf=6.0, ca=5.0)
    exp = expected_corners(a, b)
    assert exp is not None
    lam, _ = exp
    leg = screen_corners(fx(), a, b)
    assert leg is not None
    line = float(leg.selection.split()[1])
    assert line < lam, "the line must carry a cushion, not sit on expectation"
    assert CORNER_LINE_RANGE[0] <= line <= CORNER_LINE_RANGE[1]


def test_line_is_clamped_to_what_a_book_lists():
    absurd = stat(10, cf=30.0, ca=30.0)
    leg = screen_corners(fx(), absurd, absurd)
    assert leg is not None
    assert float(leg.selection.split()[1]) <= CORNER_LINE_RANGE[1]


# ── Marking ──────────────────────────────────────────────────────────────


def test_every_extra_leg_is_flagged_unvalidated_and_carries_its_sample():
    leg = screen_corners(fx(), stat(10), stat(11))
    assert leg is not None
    assert leg.validated is False
    assert leg.is_extra is True
    assert leg.sample == 10
    assert "UNVALIDATED" in leg.rationale


def test_team_total_uses_side_wording_the_booker_can_resolve():
    """"Porto Over 0.5" resolves to nothing — SportyBet never puts the club
    name in a team-total market description, so the leg would be dropped as
    unmatched. "Home Over 0.5" resolves to market 19. The club name lives on
    `market` so the digest still reads naturally."""
    picks = screen_team_goals(fx(), team_form(10, gf=2.6), team_form(11, gf=2.4))
    assert picks
    for p in picks:
        assert p.selection.startswith(("Home ", "Away ")), p.selection
        assert p.validated is False
    assert any("Home1" in p.market for p in picks)


def test_team_total_wording_actually_resolves_against_a_real_market_list():
    """The guard that matters: a change to this wording that stops resolving
    would otherwise only show up as legs silently missing from the slip."""
    from core.market_mapper import resolve_by_description

    def mk(mid, desc, spec):
        return {"id": mid, "desc": desc, "specifier": spec, "status": 0,
                "outcomes": [{"id": "12", "desc": "Over", "odds": "1.5", "isActive": 1},
                             {"id": "13", "desc": "Under", "odds": "2.5", "isActive": 1}]}

    markets = [
        mk("18", "Over/Under", "total=1.5"),
        mk("19", "Total - Home", "total=0.5"),
        mk("20", "Total - Away", "total=0.5"),
        mk("166", "Corners - Over/Under", "total=9.5"),
        mk("60182", "Bookings - Home Total", "total=1.5"),
    ]
    home = resolve_by_description("Home Over 0.5", markets)
    assert home and home["marketId"] == "19"
    away = resolve_by_description("Away Over 0.5", markets)
    assert away and away["marketId"] == "20"
    corners = resolve_by_description("Over 9.5 Corners", markets)
    assert corners and corners["marketId"] == "166"
    # The club-name wording is exactly what does NOT work.
    assert resolve_by_description("Porto Over 0.5", markets) is None


# ── Rate maths ───────────────────────────────────────────────────────────


def test_corner_probability_falls_as_the_line_rises():
    a, b = stat(10), stat(11)
    low = corner_over_probability(a, b, 7.5)
    high = corner_over_probability(a, b, 12.5)
    assert low and high
    assert low[0] > high[0]


def test_stat_forms_read_the_side_the_team_actually_played():
    """A team's corners_for must come from its own column, home or away. This
    is the ANALYSIS.md §6 swap the whole module exists to make impossible."""
    events = {
        10: [
            {"id": 1, "status": {"type": "finished"},
             "homeTeam": {"id": 10}, "awayTeam": {"id": 99}},
            {"id": 2, "status": {"type": "finished"},
             "homeTeam": {"id": 99}, "awayTeam": {"id": 10}},
        ]
    }
    stats = {
        1: {"home_corners": 8, "away_corners": 2},   # team 10 at home: 8 for
        2: {"home_corners": 3, "away_corners": 9},   # team 10 away: 9 for
    }
    forms = build_stat_forms(events, stats)
    assert forms[10].corner_matches == 2
    assert forms[10].corners_for == pytest.approx((8 + 9) / 2)
    assert forms[10].corners_against == pytest.approx((2 + 3) / 2)


def test_unfinished_matches_are_not_counted():
    events = {10: [{"id": 1, "status": {"type": "inprogress"},
                    "homeTeam": {"id": 10}, "awayTeam": {"id": 99}}]}
    forms = build_stat_forms(events, {1: {"home_corners": 8, "away_corners": 2}})
    assert forms[10].corner_matches == 0


# ── The mixed card ───────────────────────────────────────────────────────


def pick(i, sel, conv, validated=True, tier=1):
    return Pick(fixture=fx(i), market="m", selection=sel, probability=conv,
                conviction=conv, rationale="r", tier=tier, validated=validated)


def test_card_never_puts_two_legs_on_one_fixture():
    """SportyBet rejects two selections from the same match in a normal acca,
    so this is a booking failure, not merely a correlation."""
    cands = [pick(1, "Over 1.5", 0.9), pick(1, "Over 8.5 Corners", 0.8, False),
             pick(2, "Over 1.5", 0.85), pick(2, "GG", 0.7)]
    card = build_mixed_card(cands, limit=4, max_per_market=5)
    ids = [p.fixture.match_id for p in card]
    assert len(ids) == len(set(ids))


def test_card_respects_the_unvalidated_ceiling():
    cands = []
    for i in range(1, 11):
        cands.append(pick(i, "Over 8.5 Corners", 0.9, validated=False))
        cands.append(pick(i, "Over 1.5", 0.5))
    card = build_mixed_card(cands, limit=10, max_per_market=10, max_unvalidated=3)
    assert sum(1 for p in card if not p.validated) == 3
    assert len(card) == 10, "the ceiling must divert legs, not shorten the card"


def test_card_spreads_across_markets():
    cands = []
    for i in range(1, 13):
        cands.append(pick(i, "Over 1.5", 0.9))
        cands.append(pick(i, "GG", 0.8))
        cands.append(pick(i, "1X", 0.7))
    card = build_mixed_card(cands, limit=12, max_per_market=4)
    counts = {}
    for p in card:
        counts[p.selection] = counts.get(p.selection, 0) + 1
    assert max(counts.values()) <= 4
    assert len(counts) >= 3, "a 12-leg card should not be one market"


def test_card_fills_rather_than_shipping_short():
    """A cap that starves the card must top back up: a Top 20 that quietly
    returns 11 legs changes the payout by more than diversification is worth."""
    cands = [pick(i, "Over 1.5", 0.9) for i in range(1, 21)]
    card = build_mixed_card(cands, limit=20, max_per_market=4)
    assert len(card) == 20


def test_topup_does_not_discard_the_caps_it_relaxes():
    """A short card is bad; a card that silently ignores both ceilings is
    worse. When every fixture must contribute, the top-up must still prefer a
    validated leg and the least-used market."""
    cands = []
    for i in range(1, 16):
        cands.append(pick(i, "Home Over 0.5", 0.88, validated=False))
        cands.append(pick(i, "Over 8.5 Corners", 0.68, validated=False))
        cands.append(pick(i, "Over 1.5", 0.80))
    card = build_mixed_card(cands, limit=15, max_per_market=5, max_unvalidated=6)

    assert len(card) == 15, "must not ship short"
    unvalidated = sum(1 for p in card if not p.validated)
    # The ceiling cannot always be met when every fixture must contribute, but
    # it must still bind: the old code put 9 of 15 here.
    assert unvalidated <= 8, f"{unvalidated} unvalidated legs slipped through"
    counts = {}
    for p in card:
        counts[p.selection] = counts.get(p.selection, 0) + 1
    assert len(counts) >= 3, f"card collapsed onto {counts}"


def test_low_conviction_market_still_reaches_the_card_when_there_is_slack():
    """Corners always rank last within a fixture, so a pure best-per-fixture
    rule never books one. The per-market cap is what makes room for them."""
    cands = []
    for i in range(1, 21):
        cands.append(pick(i, "Home Over 0.5", 0.85, validated=False))
        cands.append(pick(i, "Over 8.5 Corners", 0.66, validated=False))
    card = build_mixed_card(cands, limit=10, max_per_market=5, max_unvalidated=10)
    sels = {p.selection for p in card}
    assert "Over 8.5 Corners" in sels, "the cap must open the door for corners"


def test_cap_counts_market_families_not_selection_strings():
    """"Home Over 0.5" and "Away Over 0.5" are two strings but one market and
    one model. Counted separately they took double any other market's budget
    and pushed corners off the card entirely."""
    cands = []
    for i in range(1, 21):
        side = "Home" if i % 2 else "Away"
        cands.append(pick(i, f"{side} Over 0.5", 0.86, validated=False))
        cands.append(pick(i, f"Over {8 + i % 3}.5 Corners", 0.70, validated=False))
    # 8 legs, two families, cap 4 — satisfiable, so the cap must hold exactly.
    card = build_mixed_card(cands, limit=8, max_per_market=4, max_unvalidated=12)

    families = {}
    for p in card:
        families[p.family] = families.get(p.family, 0) + 1
    assert families.get("team_goals", 0) <= 4, families
    assert families.get("corners", 0) >= 1, "corners must still reach the card"
    # Counting the raw strings, "Home Over 0.5" and "Away Over 0.5" would each
    # have had their own budget of 4 and filled the card between them.
    assert len(card) == 8


def test_family_is_the_selection_for_the_measured_seven():
    """Capping behaviour for the existing markets must be unchanged."""
    assert pick(1, "Over 1.5", 0.8).family == "Over 1.5"
    assert pick(1, "GG", 0.8).family == "GG"
    assert pick(1, "1X", 0.8).family == "1X"
    assert pick(1, "Over 8.5 Corners", 0.7, False).family == "corners"
    assert pick(1, "Home Over 1.5", 0.7, False).family == "team_goals"
    assert pick(1, "Over 6.5 Shots On Target", 0.7, False).family == "shots_on_target"
