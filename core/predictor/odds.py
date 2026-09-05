"""
Odds attachment and value scoring for SportCrawl's prediction engine.

The screener ranks by how *likely* a selection is. That is only half the
question — a 75% shot priced at 1.20 is a losing bet, and a 65% shot priced at
1.90 is a good one. This module fetches the live SportyBet price for each pick
and computes the edge between the model's probability and the market's.

Prices come from the same API the booker uses, so an odds lookup here confirms
the selection is actually available before it is ever sent to be booked.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from core.market_mapper import resolve_market_selection
from core.predictor.screen import Pick
from core.prediction_parser import parse_prediction_line
from core.team_matcher import match_fixture
from services.sportybet_service import SportyBetBookerService

logger = logging.getLogger("SportCrawl.Predictor.Odds")

# Shrink model probabilities toward an even-money prior before comparing them
# against a market price:
#
#   calibrated = 0.5 + SHRINK * (raw - 0.5)
#
# Currently disabled (1.0). A 142-pick backtest over 8 matchdays found the
# screener well calibrated above its 0.75 floor — 80%+ picks landed 84%, and
# 75-80% picks landed 79%. Overconfidence was confined to the 70-75% band
# (predicted ~72%, landed 63%), and raising PROBABILITY_FLOOR to 0.75 removes
# that band outright, which is a cleaner correction than distorting the bands
# that are already accurate.
#
# Re-fit from scratchpad/multiday.py if a larger sample shows drift.
CALIBRATION_SHRINK = 1.0


def calibrate(probability: float) -> float:
    """Shrink a model probability toward 0.5 to correct known overconfidence."""
    return 0.5 + CALIBRATION_SHRINK * (probability - 0.5)


@dataclass
class PricedPick:
    """A screened pick with the market's price attached."""

    pick: Pick
    odds: Optional[float] = None
    error: Optional[str] = None
    # SportyBet's id for the matched fixture. Kept because re-finding it later
    # costs a full paginated sweep of ~1029 events, and anything that wants to
    # re-price this selection — a closing-line capture above all — needs only
    # this id to go straight to factsCenter/event.
    event_id: Optional[str] = None

    @property
    def implied_probability(self) -> Optional[float]:
        """The probability the price implies, margin included."""
        if not self.odds or self.odds <= 1.0:
            return None
        return 1.0 / self.odds

    @property
    def calibrated_probability(self) -> float:
        """The model's probability corrected for its measured overconfidence."""
        return calibrate(self.pick.probability)

    @property
    def edge(self) -> Optional[float]:
        """
        Calibrated probability minus implied probability.

        Positive means the model rates the selection more likely than the price
        does. It is not free money — it is only as good as the model, and the
        bookmaker's margin is already baked into the implied side.
        """
        implied = self.implied_probability
        if implied is None:
            return None
        return self.calibrated_probability - implied

    @property
    def raw_edge(self) -> Optional[float]:
        """Edge before the calibration correction, for comparison."""
        implied = self.implied_probability
        if implied is None:
            return None
        return self.pick.probability - implied

    @property
    def expected_value(self) -> Optional[float]:
        """Return per unit staked: >1.0 is positive expectation, if the model is right."""
        if not self.odds:
            return None
        return self.calibrated_probability * self.odds


async def attach_odds(
    picks: list[Pick],
    country_code: str = "ng",
    service: Optional[SportyBetBookerService] = None,
) -> list[PricedPick]:
    """
    Look up the live SportyBet price for every pick.

    Picks whose fixture or market cannot be found are returned with ``odds``
    unset and ``error`` populated rather than dropped, so the caller can see
    what was unavailable instead of silently losing selections.
    """
    if not picks:
        return []

    service = service or SportyBetBookerService(country_code=country_code)

    logger.info("Fetching SportyBet events to price %d picks...", len(picks))
    events = await service.fetch_available_events()
    if not events:
        logger.warning("SportyBet returned no events; picks cannot be priced.")
        return [PricedPick(pick=p, error="SportyBet event list unavailable") for p in picks]

    # Cache markets per event — several picks can share one fixture.
    markets_cache: dict[str, list] = {}
    priced: list[PricedPick] = []

    for pick in picks:
        # Round-tripping through the parser guarantees we price exactly what
        # the booker will later be asked to book.
        bet = parse_prediction_line(pick.line)
        if not bet:
            priced.append(PricedPick(pick=pick, error=f"Generated line does not parse: {pick.line!r}"))
            continue

        matched = match_fixture(
            bet.home_team, bet.away_team, events,
            threshold=0.48, kickoff=getattr(pick.fixture, "start_utc", None),
        )
        if not matched:
            priced.append(PricedPick(pick=pick, error="Fixture not listed on SportyBet"))
            continue

        event_id = matched[0].get("eventId")
        if not event_id:
            priced.append(PricedPick(pick=pick, error="Matched event has no eventId"))
            continue

        if event_id not in markets_cache:
            markets_cache[event_id] = await service.fetch_event_markets(event_id)
        markets = markets_cache[event_id]

        resolved = resolve_market_selection(bet, markets)
        if not resolved:
            priced.append(
                PricedPick(pick=pick, event_id=event_id, error="Market not active on SportyBet")
            )
            continue

        try:
            odds = float(resolved.get("odds") or 0) or None
        except (TypeError, ValueError):
            odds = None

        priced.append(
            PricedPick(
                pick=pick,
                odds=odds,
                event_id=event_id,
                error=None if odds else "Market resolved but carried no price",
            )
        )

    with_price = sum(1 for p in priced if p.odds)
    logger.info("Priced %d/%d picks", with_price, len(priced))
    return priced


def filter_by_edge(priced: list[PricedPick], min_edge: float = 0.0) -> list[PricedPick]:
    """
    Keep only priced picks whose model edge clears ``min_edge``, best first.

    Unpriced picks are dropped: without a price there is nothing to judge value
    against, and including them would silently mix two different questions.
    """
    qualifying = [p for p in priced if p.edge is not None and p.edge >= min_edge]
    qualifying.sort(key=lambda p: p.edge or 0.0, reverse=True)
    return qualifying
