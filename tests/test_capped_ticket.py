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
