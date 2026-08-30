"""
Accumulator ticket construction for the draw track.

Ten draws is a good *screening* target and a terrible *ticket*. At an
optimistic 32% per leg the arithmetic is not close::

    legs   P(hit)      1 in N days   payout at 3.20/leg   frequency
    3      3.28%       31            33x                  monthly
    4      1.05%       95            105x                 3-4x / year
    5      0.34%       298           336x                 ~once / year
    6      0.11%       931           1,074x               once / 2.5 years
    8      0.011%      9,095         10,995x              once / 25 years
    10     0.0011%     88,818        112,590x             once / 243 years

A single 10-fold played daily has roughly a 0.4% chance of landing once in a
full year. A 5-fold lands about once a year, which is the actual target. So the
screener still produces ten picks; this module spends them as a ladder of
shorter tickets instead of one unreachable accumulator.

Disjoint by default
-------------------
Tickets share no legs unless ``disjoint=False``. Overlapping tickets die
together — one bad leg kills every ticket containing it — which is precisely
the wrong failure mode when the goal is for *something* to land.

On edge compounding
-------------------
Accumulators multiply edge as well as margin. If each leg really carries 2.4%
edge (32% model against 31.25% implied at 3.20), a 5-fold returns 1.024^5 =
+12.6% and a 10-fold +26.8%. That is the honest upside, and it is why this is
not the same conversation as the 1X accumulators, whose legs were priced at or
below break-even.

The corresponding honest downside: compounding amplifies estimation error by
the same exponent. No draw pick in this repo has ever been graded, so the sign
of the per-leg edge is unknown, not merely its size. If the true edge is -2%
rather than +2.4%, a 10-fold returns 0.82 per stake. Short tickets while the
sample builds.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, Sequence

from core.predictor.odds import PricedPick
from core.predictor.screen import Pick

logger = logging.getLogger("SportCrawl.Predictor.Tickets")

# Default ladder: two five-folds off ten picks. Each lands about once a year on
# its own, so the pair is expected to land roughly 2.4 times a year while using
# exactly the ten selections the screener was asked for.
DEFAULT_SHAPE = (5, 5)

# SportyBet caps the payout on a single ticket. The figure is not hardcoded
# here because it varies by market and changes without notice — read it off the
# account and pass it in.
#
# It matters more than it looks. A 10-fold at 112,590x nominally turns a 1,000
# stake into 112.6m, which the cap truncates; every unit of stake above
# cap/odds is being placed at zero return. Positive expected value on paper
# becomes negative in the account. Short tickets stay clear of it.
DEFAULT_MAX_PAYOUT: Optional[float] = None


@dataclass
class Ticket:
    """One accumulator: a set of legs and what they are collectively worth."""

    legs: list[Pick]
    leg_odds: list[Optional[float]] = field(default_factory=list)
    label: str = ""

    @property
    def size(self) -> int:
        return len(self.legs)

    @property
    def combined_probability(self) -> float:
        """
        Joint probability, assuming legs are independent.

        Fixtures are distinct matches, so independence is a fair first
        approximation. It is not exact — same-league legs share weather,
        refereeing and round-level effects — and any such correlation is
        positive, which makes this figure a mild *under*estimate.
        """
        p = 1.0
        for leg in self.legs:
            p *= leg.probability
        return p

    @property
    def combined_odds(self) -> Optional[float]:
        """Product of the leg prices, or ``None`` if any leg is unpriced."""
        if not self.leg_odds or len(self.leg_odds) != len(self.legs):
            return None
        if any(o is None or o <= 1.0 for o in self.leg_odds):
            return None
        product = 1.0
        for o in self.leg_odds:
            product *= o  # type: ignore[operator]
        return product

    @property
    def expected_value(self) -> Optional[float]:
        """Return per unit staked. Above 1.0 is positive expectation."""
        odds = self.combined_odds
        if odds is None:
            return None
        return self.combined_probability * odds

    @property
    def one_in(self) -> Optional[float]:
        """Expected number of attempts per hit."""
        p = self.combined_probability
        return 1.0 / p if p > 0 else None

    @property
    def expected_hits_per_year(self) -> float:
        """Hits expected from playing this ticket shape once a day for a year."""
        return 365.0 * self.combined_probability

    def max_useful_stake(self, max_payout: Optional[float] = DEFAULT_MAX_PAYOUT) -> Optional[float]:
        """
        Largest stake whose full winnings fit under the payout cap.

        Stake above this is dead money: it increases the loss on every losing
        ticket and adds nothing to the win.
        """
        odds = self.combined_odds
        if max_payout is None or odds is None or odds <= 0:
            return None
        return max_payout / odds

    @property
    def lines(self) -> list[str]:
        """Booker-ready selection lines for this ticket."""
        return [leg.line for leg in self.legs]


def build_ladder(
    priced: Sequence[PricedPick],
    shape: Sequence[int] = DEFAULT_SHAPE,
    disjoint: bool = True,
) -> list[Ticket]:
    """
    Spend a ranked list of priced picks as a ladder of accumulators.

    ``shape`` is the leg count of each ticket in order, e.g. ``(5, 5)`` for two
    five-folds or ``(5, 4)`` to hold one pick back. Picks are consumed
    best-first, so the first ticket in the ladder is the strongest.

    Tickets that cannot be filled are skipped rather than shortened — a
    four-fold silently emitted where a five was asked for changes the payout by
    a factor of three, which is not a detail to paper over.
    """
    pool = [p for p in priced if p.odds]
    if not pool:
        logger.warning("No priced picks available; no tickets built.")
        return []

    tickets: list[Ticket] = []
    cursor = 0

    for index, size in enumerate(shape, 1):
        if disjoint:
            window = pool[cursor : cursor + size]
            cursor += size
        else:
            window = pool[:size]

        if len(window) < size:
            logger.warning(
                "Ticket %d wanted %d legs, only %d picks left — skipped.",
                index,
                size,
                len(window),
            )
            continue

        tickets.append(
            Ticket(
                legs=[p.pick for p in window],
                leg_odds=[p.odds for p in window],
                label=f"{size}-fold #{index}",
            )
        )

    return tickets


def format_ladder(tickets: list[Ticket], max_payout: Optional[float] = DEFAULT_MAX_PAYOUT) -> str:
    """Render the ladder for review before anything is booked."""
    if not tickets:
        return "No tickets built."

    lines = [
        "=" * 78,
        "DRAW TICKET LADDER",
        "=" * 78,
    ]

    for ticket in tickets:
        odds = ticket.combined_odds
        ev = ticket.expected_value
        one_in = ticket.one_in

        lines.append("")
        lines.append(f"{ticket.label}   {ticket.size} legs")
        lines.append("-" * 78)
        for leg, price in zip(ticket.legs, ticket.leg_odds):
            match = leg.fixture.label
            if len(match) > 44:
                match = match[:41] + "..."
            price_text = f"{price:.2f}" if price else "—"
            lines.append(f"  {match:<46} {leg.probability:>5.1%} @ {price_text:>6}")

        lines.append("-" * 78)
        lines.append(
            f"  joint probability {ticket.combined_probability:.3%}"
            + (f"   1 in {one_in:,.0f}" if one_in else "")
            + f"   {ticket.expected_hits_per_year:.2f} hits/year at one a day"
        )
        if odds:
            lines.append(
                f"  combined odds {odds:,.2f}x"
                + (f"   expected value {ev:.3f} per unit staked" if ev else "")
            )
            stake_cap = ticket.max_useful_stake(max_payout)
            if stake_cap is not None:
                lines.append(
                    f"  payout cap bites above a stake of {stake_cap:,.2f} — "
                    f"anything larger returns nothing extra on a win"
                )
        else:
            lines.append("  combined odds unavailable — at least one leg was unpriced")

    lines.append("")
    lines.append("=" * 78)
    lines.append("BOOKER INPUT")
    lines.append("=" * 78)
    for ticket in tickets:
        lines.append(f"# {ticket.label}")
        lines.extend(ticket.lines)
        lines.append("")

    if max_payout is None:
        lines.append(
            "Payout cap not configured — pass --max-payout with the figure from your "
            "SportyBet account to see where stake stops earning."
        )

    return "\n".join(lines)


def build_capped_ticket(
    priced: Sequence[PricedPick],
    max_combined_odds: float = 2.0,
    max_legs: int = 3,
    label: str = "2 Odds",
) -> Optional[Ticket]:
    """
    The strongest short accumulator that stays under a price cap.

    Legs are taken highest-probability first and added only while the running
    product stays at or under ``max_combined_odds``. A leg too expensive to fit
    is SKIPPED rather than ending the search: stopping at the first one that
    does not fit would end most tickets at a single leg, because the very
    shortest prices are not always the most probable selections.

    Returns ``None`` if nothing is priced — an unpriced ticket has no combined
    odds, so a cap could not be honoured and claiming otherwise would be a lie
    about the one property the caller asked for.
    """
    pool = [p for p in priced if p.odds and p.odds > 1.0]
    if not pool:
        logger.warning("No priced picks available; no capped ticket built.")
        return None

    pool.sort(key=lambda p: p.pick.probability, reverse=True)

    legs: list[Pick] = []
    leg_odds: list[Optional[float]] = []
    seen_fixtures: set[tuple[str, str]] = set()
    combined = 1.0

    for cand in pool:
        if len(legs) >= max_legs:
            break
        # One leg per fixture. screen_fixtures already enforces this upstream,
        # but two selections on the same match are correlated and a bookmaker
        # will not accept them on one slip, so do not depend on it.
        key = (cand.pick.fixture.home_name, cand.pick.fixture.away_name)
        if key in seen_fixtures:
            continue
        if combined * cand.odds > max_combined_odds:
            continue
        legs.append(cand.pick)
        leg_odds.append(cand.odds)
        seen_fixtures.add(key)
        combined *= cand.odds

    if not legs:
        logger.warning(
            "No leg priced at or under %.2f; no capped ticket built.", max_combined_odds
        )
        return None

    logger.info(
        "Capped ticket: %d leg(s) at %.2f combined (cap %.2f).",
        len(legs), combined, max_combined_odds,
    )
    return Ticket(legs=legs, leg_odds=leg_odds, label=label)
