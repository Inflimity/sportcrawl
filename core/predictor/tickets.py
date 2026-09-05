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

import itertools
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
    # Set when the ticket is not the shape that was asked for — too few legs
    # fitted under the cap, say. Surfaced in the digest so a short ticket is
    # never mistaken for the intended one.
    note: str = ""

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


# Fraction of the cap a ticket must reach before it counts as having filled it.
# A "2 odds" ticket that returns 1.30 is not the product asked for, so a longer
# combination that actually approaches 2.00 is preferred over a shorter, safer
# one that does not.
DEFAULT_TARGET_RATIO = 0.85

# No leg below this shrunk probability may enter a banker, whatever it does for
# the combined price. The cap is a payout target, not a licence to pad.
DEFAULT_MIN_LEG_PROBABILITY = 0.5

# Largest candidate pool the exhaustive search will consider. Combinations grow
# as C(n, legs); 40 candidates at 3 legs is under 10k evaluations, which is
# nothing, and the cap is only ever filled from the top of the ranking anyway.
MAX_SEARCH_POOL = 40


def _fixture_key(pick: Pick) -> tuple[str, str]:
    return (pick.fixture.home_name, pick.fixture.away_name)


def build_capped_ticket(
    priced: Sequence[PricedPick],
    max_combined_odds: float = 2.0,
    max_legs: int = 3,
    min_legs: int = 2,
    label: str = "2 Odds",
    target_ratio: float = DEFAULT_TARGET_RATIO,
    min_leg_probability: float = DEFAULT_MIN_LEG_PROBABILITY,
    max_per_market: int = 0,
) -> Optional[Ticket]:
    """
    The safest short accumulator that actually reaches a price cap.

    Two objectives are in tension and neither survives alone.

    Maximising joint probability degenerates: every extra leg multiplies in a
    number below one, so "safest" always answers with the fewest legs allowed.
    Maximising expected value degenerates the same way, because the bookmaker's
    margin means a leg is typically priced above its true probability, so
    ``p x odds < 1`` and adding one lowers the product. Either objective on its
    own returns a single leg — which is exactly the behaviour this replaced.

    So leg count is treated as what it is: a target, not something to optimise.
    Combinations that reach ``target_ratio`` of the cap are the ones that
    deliver the product asked for, and the safest of *those* wins. Only when
    none reaches it does the search fall back to whichever combination gets
    closest to the cap.

    Legs are constrained to one per fixture, to at least ``min_legs``, and to
    selections above ``min_leg_probability`` — a cap is a payout target, not a
    reason to pad the slip with a coin flip.

    ``max_per_market`` additionally limits how many legs may share a selection.
    It defaults to 0 (off), and that default is a judgement about evidence, not
    an oversight: form-picked Over 1.5 is the only market on this engine with a
    measured edge (81.5% against a 77.2% base over 297 legs), while GG has never
    been graded at all. Forcing variety today would trade a measured market for
    an unmeasured one. Set it once the other markets have numbers.

    Returns ``None`` if nothing is priced: an unpriced ticket has no combined
    odds, so a cap could not be honoured and claiming otherwise would be a lie
    about the one property the caller asked for.
    """
    pool = [
        p for p in priced
        if p.odds and p.odds > 1.0 and p.pick.probability >= min_leg_probability
    ]
    if not pool:
        logger.warning(
            "No priced pick cleared the %.0f%% leg floor; no capped ticket built.",
            min_leg_probability * 100,
        )
        return None

    # Best-supported first, so the truncation below keeps the strongest reads
    # and the tie-breaks below resolve toward them.
    pool.sort(key=lambda p: p.pick.probability, reverse=True)
    pool = pool[:MAX_SEARCH_POOL]

    max_legs = max(1, max_legs)
    min_legs = max(1, min(min_legs, max_legs))
    target = max_combined_odds * target_ratio

    def evaluate(combo: Sequence[PricedPick]) -> Optional[tuple[float, float]]:
        """``(combined odds, joint probability)``, or ``None`` if invalid."""
        seen: set[tuple[str, str]] = set()
        per_market: dict[str, int] = {}
        odds = 1.0
        probability = 1.0
        for cand in combo:
            key = _fixture_key(cand.pick)
            if key in seen:
                return None
            seen.add(key)
            if max_per_market > 0:
                # Distinct fixtures settle independently, so this is not about
                # outcome variance. It is about correlated *model* error: three
                # Over 1.5 legs are one estimate entered three times, and if
                # that estimate is biased the ticket loses as a unit.
                selection = cand.pick.selection
                per_market[selection] = per_market.get(selection, 0) + 1
                if per_market[selection] > max_per_market:
                    return None
            odds *= cand.odds  # type: ignore[operator]
            if odds > max_combined_odds:
                return None
            probability *= cand.pick.probability
        return odds, probability

    filling: Optional[tuple[float, Sequence[PricedPick], float]] = None   # by probability
    fallback: Optional[tuple[float, Sequence[PricedPick], float]] = None  # by odds

    for size in range(min_legs, max_legs + 1):
        for combo in itertools.combinations(pool, size):
            scored = evaluate(combo)
            if scored is None:
                continue
            odds, probability = scored

            if odds >= target:
                if filling is None or probability > filling[0]:
                    filling = (probability, combo, odds)
            elif fallback is None or odds > fallback[0]:
                fallback = (odds, combo, probability)

    note = ""
    if filling is not None:
        chosen, combined = filling[1], filling[2]
    elif fallback is not None:
        chosen, combined = fallback[1], fallback[2]
        note = (
            f"no combination reached {target:.2f}; this is the closest to the "
            f"{max_combined_odds:.2f} cap that the priced legs allow"
        )
        logger.info("Capped ticket: nothing reached %.2f; took the closest at %.2f.", target, combined)
    else:
        # Not even min_legs fit. Ship the single best leg rather than nothing,
        # and say so — a one-leg "accumulator" that looks like the intended
        # ticket is how a risky single got mistaken for a banker before.
        single = next((p for p in pool if p.odds and p.odds <= max_combined_odds), None)
        if single is None:
            logger.warning(
                "No leg priced at or under %.2f; no capped ticket built.", max_combined_odds
            )
            return None
        chosen, combined = (single,), single.odds  # type: ignore[assignment]
        note = (
            f"only one leg could be priced under the {max_combined_odds:.2f} cap — "
            f"this is a single, not the {min_legs}-leg accumulator"
        )
        logger.warning(
            "Capped ticket: could not fit %d legs under %.2f; falling back to a single.",
            min_legs, max_combined_odds,
        )

    logger.info(
        "Capped ticket: %d leg(s) at %.2f combined (cap %.2f, target %.2f).",
        len(chosen), combined, max_combined_odds, target,
    )
    return Ticket(
        legs=[c.pick for c in chosen],
        leg_odds=[c.odds for c in chosen],
        label=label,
        note=note,
    )


def cap_per_market(
    picks: "Sequence[Pick]",
    max_per_market: int,
    limit: Optional[int] = None,
) -> list[Pick]:
    """
    Keep a ranked list from filling with a single market.

    The case for this is not outcome variance — ten distinct fixtures really do
    settle independently. It is **correlated model error**. When every leg is
    the same market, one biased estimate is not one bad leg among ten, it is
    ten bad legs. A Top 10 of nothing but Over 1.5 is a bet that the Over 1.5
    model is right, entered ten times, not a diversified card.

    That concentration is a selection-rule artefact rather than a read on the
    day: ranking by probability alone always converges on whichever market
    carries the highest base rate, and Over 1.5 is a superset of Over 2.5 so it
    wins nearly every fixture by construction.

    Order is preserved, so the best pick of each market still comes first.
    ``max_per_market <= 0`` disables the cap.
    """
    if max_per_market <= 0:
        kept = list(picks)
        return kept[:limit] if limit else kept

    counts: dict[str, int] = {}
    kept: list[Pick] = []
    overflow: list[Pick] = []

    for pick in picks:
        seen = counts.get(pick.selection, 0)
        if seen < max_per_market:
            counts[pick.selection] = seen + 1
            kept.append(pick)
        else:
            overflow.append(pick)

    # If the cap starved the list, top back up from what it rejected rather
    # than ship a short ticket. A Top 10 that quietly returns 6 legs changes
    # the payout by more than the diversification was worth.
    if limit and len(kept) < limit:
        kept.extend(overflow[: limit - len(kept)])

    logger.info(
        "Market cap %d: %d picks kept across %d markets (%d deferred).",
        max_per_market, len(kept), len(counts), len(overflow),
    )
    return kept[:limit] if limit else kept
