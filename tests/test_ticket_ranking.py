"""
Tests for price-aware ranking on the Top 10 / Top 20 path.

That path used to book on model probability alone and never see a price, which
is why it filled with a single market and why an implausible edge could not
announce itself. These cover the ranking, not the selection floors.
"""

import pytest

from core.predictor.filter import Fixture
from core.predictor.odds import PricedPick
from core.predictor.screen import Pick
from services.pipeline import PredictionBookingPipeline as P


def _priced(name: str, selection: str, probability: float, odds):
    fixture = Fixture.__new__(Fixture)
    object.__setattr__(fixture, "home_name", name)
    object.__setattr__(fixture, "away_name", "Opp")
    pick = Pick(fixture=fixture, market="M", selection=selection,
                probability=probability, conviction=probability, rationale="")
    return PricedPick(pick=pick, odds=odds)


def test_default_ranking_is_unchanged():
    """Price awareness must not silently reorder anyone's tickets."""
    pool = [_priced("A", "Over 1.5", 0.90, 1.30),
            _priced("B", "GG", 0.65, 2.10),
            _priced("C", "Over 2.5", 0.75, 1.60)]
    assert [p.fixture.home_name for p in P._rank_picks(pool)] == ["A", "B", "C"]


def test_edge_ranking_prefers_value_over_raw_probability():
    """
    B is less likely but far better priced. Ranking on probability buries it;
    ranking on edge surfaces it. This is the 'hit rate is not the goal, value
    is' distinction, made operational.
    """
    pool = [
        _priced("A", "Over 1.5", 0.90, 1.10),   # implied 90.9% -> edge -0.9pp
        _priced("B", "GG", 0.65, 2.10),         # implied 47.6% -> edge +17.4pp
    ]
    ranked = P._rank_picks(pool, rank_by_edge=True)
    assert [p.fixture.home_name for p in ranked] == ["B", "A"]


def test_min_edge_drops_picks_priced_at_or_through_fair_value():
    pool = [_priced("A", "Over 1.5", 0.90, 1.10),   # negative edge
            _priced("B", "GG", 0.65, 2.10)]         # strong edge
    kept = P._rank_picks(pool, min_edge=0.05)
    assert [p.fixture.home_name for p in kept] == ["B"]


def test_unpriced_picks_survive_the_default_path():
    """A pick SportyBet does not list is still bookable; it just has no edge."""
    pool = [_priced("A", "Over 1.5", 0.90, None)]
    assert len(P._rank_picks(pool)) == 1


def test_unpriced_picks_sort_last_when_ranking_on_edge():
    pool = [_priced("A", "Over 1.5", 0.95, None),
            _priced("B", "GG", 0.65, 2.10)]
    ranked = P._rank_picks(pool, rank_by_edge=True)
    assert [p.fixture.home_name for p in ranked] == ["B", "A"]


def test_unpriced_picks_are_dropped_by_a_min_edge_filter():
    """No price means no measurable edge, so it cannot clear an edge floor."""
    pool = [_priced("A", "Over 1.5", 0.95, None)]
    assert P._rank_picks(pool, min_edge=0.01) == []
