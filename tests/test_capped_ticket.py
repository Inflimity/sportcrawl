"""
Tests for the capped banker: the short ticket built to a price ceiling.

The contract is narrow and worth stating plainly. The ticket takes the most
probable legs it can, but the CAP is the promise — a ticket labelled "2 odds"
that prices at 2.4 is not a near miss, it is a different bet from the one the
reader thinks they are holding.
"""

import random

import pytest

from core.prediction_parser import parse_prediction_line
from core.predictor.filter import Fixture
from core.predictor.odds import PricedPick
from core.predictor.screen import Pick
from core.predictor.tickets import build_capped_ticket


def _priced(home: str, away: str, selection: str, probability: float,
            odds: float | None, market: str = "1X2") -> PricedPick:
    fixture = Fixture.__new__(Fixture)
    object.__setattr__(fixture, "home_name", home)
    object.__setattr__(fixture, "away_name", away)
    pick = Pick(fixture=fixture, market=market, selection=selection,
                probability=probability, conviction=probability * 0.9, rationale="")
    return PricedPick(pick=pick, odds=odds)


def test_builds_three_legs_under_the_cap():
    ticket = build_capped_ticket([
        _priced("Ajax", "Heerenveen", "1X", 0.86, 1.22),
        _priced("Bayern", "Augsburg", "1X", 0.84, 1.20),
        _priced("Inter", "Empoli", "1X", 0.82, 1.25),
        _priced("Arsenal", "Luton", "1X", 0.80, 1.18),
    ], max_combined_odds=2.0, max_legs=3)

    assert ticket is not None
    assert ticket.size == 3
    assert ticket.combined_odds <= 2.0
    assert ticket.combined_odds == pytest.approx(1.22 * 1.20 * 1.25)


def test_a_leg_too_expensive_is_skipped_not_fatal():
    """The most probable pick is not always the cheapest.

    Stopping at the first leg that will not fit would end most tickets at one
    leg. It must be skipped so a cheaper leg further down can complete the set.
    """
    ticket = build_capped_ticket([
        _priced("A", "B", "Over 2.5", 0.90, 2.40),   # most probable, blows the cap alone
        _priced("C", "D", "1X", 0.85, 1.30),
        _priced("E", "F", "1X", 0.83, 1.25),
        _priced("G", "H", "1X", 0.80, 1.20),
    ], max_combined_odds=2.0, max_legs=3)

    assert ticket.size == 3
    assert ticket.combined_odds <= 2.0
    assert all(leg.fixture.home_name != "A" for leg in ticket.legs)


def test_short_ticket_rather_than_a_breached_cap():
    ticket = build_capped_ticket([
        _priced("A", "B", "Over 2.5", 0.70, 1.90),
        _priced("C", "D", "GG", 0.68, 1.85),
    ], max_combined_odds=2.0, max_legs=3)

    assert ticket.size == 1
    assert ticket.combined_odds <= 2.0


def test_returns_none_when_nothing_fits():
    assert build_capped_ticket(
        [_priced("A", "B", "X", 0.40, 3.5)], max_combined_odds=2.0, max_legs=3
    ) is None


def test_returns_none_when_nothing_is_priced():
    """An unpriced ticket has no combined odds, so no cap can be honoured."""
    assert build_capped_ticket(
        [_priced("A", "B", "1X", 0.9, None)], max_combined_odds=2.0, max_legs=3
    ) is None


def test_one_leg_per_fixture():
    """Two selections on the same match are correlated and unbookable together."""
    ticket = build_capped_ticket([
        _priced("A", "B", "1X", 0.88, 1.20),
        _priced("A", "B", "Over 1.5", 0.87, 1.15),
        _priced("C", "D", "1X", 0.84, 1.25),
    ], max_combined_odds=2.0, max_legs=3)

    names = [leg.fixture.home_name for leg in ticket.legs]
    assert len(names) == len(set(names))


def test_legs_are_ordered_by_probability():
    ticket = build_capped_ticket([
        _priced("C", "D", "1X", 0.80, 1.20),
        _priced("A", "B", "1X", 0.88, 1.20),
        _priced("E", "F", "1X", 0.84, 1.20),
    ], max_combined_odds=2.0, max_legs=3)

    probs = [leg.probability for leg in ticket.legs]
    assert probs == sorted(probs, reverse=True)


def test_legs_round_trip_through_the_booker_parser():
    """Output contract: every line must parse, or the ticket cannot be booked."""
    ticket = build_capped_ticket([
        _priced("Ajax", "Heerenveen", "1X", 0.86, 1.22),
        _priced("Inter", "Empoli", "Over 2.5", 0.82, 1.55, market="Total Goals"),
    ], max_combined_odds=2.0, max_legs=3)

    for line in ticket.lines:
        assert parse_prediction_line(line) is not None, line


def test_cap_holds_across_randomised_cards():
    random.seed(7)
    for _ in range(500):
        pool = [
            _priced(f"H{i}", f"A{i}", "1X",
                    random.uniform(0.50, 0.95), random.uniform(1.05, 3.0))
            for i in range(12)
        ]
        ticket = build_capped_ticket(pool, max_combined_odds=2.0, max_legs=3)
        if ticket:
            assert ticket.size <= 3
            assert ticket.combined_odds <= 2.0


# ── The single-leg bug ──────────────────────────────────────────────────
#
# The builder used to be greedy: take the most probable leg, then only add
# legs that still fit. When the most probable leg was also a long price, it
# ate the whole budget and the "accumulator" shipped as one game. Worse, that
# one game was whatever market the form rated highest — an Over 2.5 needing
# three goals could be presented as the day's banker.


def test_never_ships_a_single_when_two_legs_fit():
    """The greedy build took the 1.55 first and had no room left. This must not."""
    ticket = build_capped_ticket([
        _priced("Alpha", "Beta", "Over 2.5", 0.86, 1.55),   # most probable, pricey
        _priced("Gamma", "Delta", "Over 1.5", 0.80, 1.26),
        _priced("Eps", "Zeta", "Over 1.5", 0.79, 1.30),
    ], max_combined_odds=2.0, max_legs=3, min_legs=2)

    assert ticket.size >= 2
    assert ticket.combined_odds <= 2.0
    assert not ticket.note


def test_min_legs_is_respected_over_raw_probability():
    """
    Two legs beat one even though one leg is strictly more probable.

    Maximising joint probability alone always answers "fewest legs", which is
    the degeneracy that produced the single. The cap is a target, not just a
    ceiling.
    """
    ticket = build_capped_ticket([
        _priced("A", "B", "Over 1.5", 0.90, 1.90),
        _priced("C", "D", "Over 1.5", 0.85, 1.25),
        _priced("E", "F", "Over 1.5", 0.84, 1.28),
    ], max_combined_odds=2.0, max_legs=3, min_legs=2)

    assert ticket.size == 2


def test_a_ticket_must_actually_reach_the_cap():
    """A '2 odds' ticket returning 1.30 is not the product asked for."""
    ticket = build_capped_ticket([
        _priced("A", "B", "Over 1.5", 0.88, 1.12),
        _priced("C", "D", "Over 1.5", 0.87, 1.14),
        _priced("E", "F", "Over 1.5", 0.86, 1.40),
    ], max_combined_odds=2.0, max_legs=3, min_legs=2)

    # The two safest legs make only 1.28. Reaching 0.85 x 2.00 = 1.70 needs
    # the third, and the builder must take it rather than bank the safer 1.28.
    assert ticket.combined_odds >= 1.70
    assert ticket.combined_odds <= 2.0


def test_a_forced_single_is_labelled_as_one():
    """If only one leg fits, say so — do not present it as the banker ticket."""
    ticket = build_capped_ticket([
        _priced("A", "B", "Over 1.5", 0.88, 1.20),
        _priced("C", "D", "Over 1.5", 0.87, 1.30),
    ], max_combined_odds=1.35, max_legs=3, min_legs=2)

    assert ticket.size == 1
    assert "single" in ticket.note


def test_a_coin_flip_never_pads_the_slip():
    """Reaching the cap is not a licence to add a leg below the quality floor."""
    ticket = build_capped_ticket([
        _priced("A", "B", "Over 1.5", 0.86, 1.30),
        _priced("C", "D", "Over 1.5", 0.85, 1.32),
        _priced("E", "F", "X", 0.31, 1.15),   # cheap, and nowhere near a banker
    ], max_combined_odds=2.0, max_legs=3, min_legs=2, min_leg_probability=0.5)

    assert all(leg.probability >= 0.5 for leg in ticket.legs)


def test_min_legs_holds_across_randomised_cards():
    random.seed(11)
    for _ in range(300):
        card = [
            _priced(f"H{i}", f"A{i}", "Over 1.5",
                    round(random.uniform(0.55, 0.90), 2),
                    round(random.uniform(1.10, 1.45), 2))
            for i in range(8)
        ]
        ticket = build_capped_ticket(card, max_combined_odds=2.0, max_legs=3, min_legs=2)
        assert ticket is not None
        assert ticket.combined_odds <= 2.0 + 1e-9
        # Eight legs between 1.10 and 1.45 always admit a pair under 2.00.
        assert ticket.size >= 2 and not ticket.note


# ── Per-market cap ──────────────────────────────────────────────────────
#
# Ten legs in one market is not ten independent bets on ten fixtures; it is
# one bet on one model, entered ten times. The cap bounds correlated model
# error, which diversifying across fixtures alone does not.


def _pick(home: str, away: str, selection: str, probability: float):
    return _priced(home, away, selection, probability, 1.25).pick


def test_cap_limits_how_many_legs_share_a_market():
    from core.predictor.tickets import cap_per_market

    picks = [_pick(f"H{i}", f"A{i}", "Over 1.5", 0.9 - i * 0.01) for i in range(10)]
    picks += [_pick(f"G{i}", f"B{i}", "GG", 0.7 - i * 0.01) for i in range(5)]

    kept = cap_per_market(picks, max_per_market=6, limit=10)
    assert sum(p.selection == "Over 1.5" for p in kept) == 6
    assert len(kept) == 10


def test_cap_preserves_ranking_within_a_market():
    from core.predictor.tickets import cap_per_market

    picks = [_pick(f"H{i}", f"A{i}", "Over 1.5", 0.9 - i * 0.01) for i in range(5)]
    kept = cap_per_market(picks, max_per_market=3)
    assert [p.probability for p in kept] == pytest.approx([0.90, 0.89, 0.88])


def test_cap_tops_back_up_rather_than_shipping_a_short_ticket():
    """A Top 10 that quietly returns 6 changes the payout more than the cap helps."""
    from core.predictor.tickets import cap_per_market

    picks = [_pick(f"H{i}", f"A{i}", "Over 1.5", 0.9) for i in range(10)]
    kept = cap_per_market(picks, max_per_market=4, limit=10)
    assert len(kept) == 10


def test_cap_of_zero_is_a_no_op():
    from core.predictor.tickets import cap_per_market

    picks = [_pick(f"H{i}", f"A{i}", "Over 1.5", 0.9) for i in range(5)]
    assert cap_per_market(picks, max_per_market=0) == picks
