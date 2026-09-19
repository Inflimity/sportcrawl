"""
Bet builder sheet: every selection in a fixture, and what a combination is
really worth.

WHAT THIS IS FOR
----------------
You pick a handful of fixtures. For each one this prints every outcome we can
price, and then — the part that matters — prices COMBINATIONS of them using how
often those outcomes actually co-occur, not by multiplying.

Multiplying is the mistake a bet builder is designed to profit from:

    HOME + HT_HOME    naive 16.1%   actual 29.2%   you are 1.8x too pessimistic
    U2.5 + BTTS       naive 20.5%   actual  7.5%   you are 2.7x too optimistic

WHAT IT DELIBERATELY WILL NOT DO
--------------------------------
It will not tell you a builder is a good bet, because on the evidence available
it usually is not, and the arithmetic is not close:

  - a single carries 4-6% margin; a bet builder carries 15-25%
  - five 3-leg builders combined keep about a third of fair value
  - the one edge this project has validated (form_pick, 81.5% vs a 77.2% base,
    +5.6%) is worth +1.6% per leg at the 3.8% margin of match goals, and turns
    NEGATIVE at the 5.8-6.0% of corners

So the sheet always prints the required price alongside the fair price. If the
book is not offering better than `fair_price`, the combination loses money, and
the size of the gap is the whole decision.

CORNERS
-------
Corner selections are listed as UNPRICEABLE until match_stats.py has banked a
sample. That is not an oversight — ANALYSIS.md §8 found no repeatable corner
edge from season averages, and inventing a corner probability from a handful of
matches would reproduce the §2 error (a maximum taken over noise) in a new
place. They appear in the sheet so you can see what is available to book; they
carry no number until the number is real.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from core.predictor.correlation import MARKETS, CorrelationTable

# Selections a bookmaker offers on a builder that we cannot yet price. Listed so
# the sheet is honest about the difference between "unlikely" and "unknown".
UNPRICEABLE = {
    "CORNERS_O8.5", "CORNERS_O9.5", "CORNERS_O10.5", "CORNERS_O11.5",
    "HOME_CORNERS_O4.5", "AWAY_CORNERS_O4.5", "CORNERS_1X2",
    "ANYTIME_SCORER", "CARDS_O3.5", "SHOTS_ON_TARGET_O8.5",
}

# Bet builder margin, as a fraction. The research range is 15-25%; 20% is the
# midpoint and the default. Override it per book once you have measured a real
# one with scripts/margin.py against actual builder prices.
DEFAULT_BUILDER_MARGIN = 0.20


@dataclass
class Selection:
    """One outcome in one fixture."""

    market: str
    probability: Optional[float]      # None when unpriceable
    note: str = ""

    @property
    def fair_odds(self) -> Optional[float]:
        if not self.probability:
            return None
        return 1.0 / self.probability


@dataclass
class Combination:
    """
    Two or more selections from the SAME fixture, priced honestly.

    ``naive_probability`` is what multiplying gives you. ``true_probability`` is
    what the history says. ``required_odds`` is the price the book must beat for
    this to be worth staking once the builder margin is taken into account.
    """

    markets: tuple[str, ...]
    naive_probability: float
    true_probability: Optional[float]
    sample: Optional[int] = None

    @property
    def ratio(self) -> Optional[float]:
        if self.true_probability is None or self.naive_probability <= 0:
            return None
        return self.true_probability / self.naive_probability

    @property
    def fair_odds(self) -> Optional[float]:
        if not self.true_probability:
            return None
        return 1.0 / self.true_probability

    def required_odds(self, margin: float = DEFAULT_BUILDER_MARGIN) -> Optional[float]:
        """
        The price at which this combination breaks even for you.

        Fair odds are what the outcome is worth. The book keeps `margin`, so a
        price below fair/(1-margin) is one where the margin has already eaten
        any edge you thought you had.
        """
        fair = self.fair_odds
        if fair is None:
            return None
        return fair / (1.0 - margin)

    @property
    def verdict(self) -> str:
        r = self.ratio
        if r is None:
            return "unpriceable — no history for this combination"
        if r >= 1.25:
            return "legs pull together; the book shortens this hardest"
        if r <= 0.80:
            return "legs pull apart; long odds here are long for a reason"
        return "close to independent; the naive price is roughly right"


@dataclass
class FixtureSheet:
    """Everything bookable in one fixture."""

    home: str
    away: str
    selections: list[Selection]
    combinations: list[Combination]

    @property
    def label(self) -> str:
        return f"{self.home} vs {self.away}"


def _true_joint(table: CorrelationTable, markets: Sequence[str]) -> tuple[Optional[float], Optional[int]]:
    """
    Joint probability of an arbitrary number of legs.

    Two legs come straight from the pairwise table. Three or more are not stored
    pairwise, so we return None rather than approximating: chaining pairwise
    ratios assumes the interactions are independent of each other, which for
    football markets is exactly the assumption that fails. A number we cannot
    stand behind is worse here than no number, because the whole point of this
    module is to stop people multiplying things that should not be multiplied.
    """
    if len(markets) == 2:
        x, y = markets
        pj = table.joint_p(x, y)
        count = table.joint.get((x, y) if x <= y else (y, x))
        return pj, count
    return None, None


def build_fixture_sheet(
    home: str,
    away: str,
    probabilities: dict[str, float],
    table: CorrelationTable,
    include_unpriceable: bool = True,
    max_combination_legs: int = 2,
) -> FixtureSheet:
    """
    Build the sheet for one fixture.

    ``probabilities`` is your model's per-market probability for this specific
    fixture — from TeamForm, opponent-adjusted per ANALYSIS.md §5. The
    correlation ``table`` supplies the SHAPE of the dependence between markets,
    measured across all matches; the fixture supplies the LEVEL.

    Combining the two this way is the point: a league-wide correlation applied
    to a fixture-specific probability is a far better estimate than either the
    league average alone or a fixture-specific joint rate computed from the
    handful of matches those two teams have played.
    """
    selections: list[Selection] = []

    for market, p in sorted(probabilities.items(), key=lambda kv: -kv[1]):
        if market not in MARKETS:
            selections.append(Selection(market, None, "not derivable from goals"))
            continue
        selections.append(Selection(market, p))

    if include_unpriceable:
        for market in sorted(UNPRICEABLE):
            selections.append(
                Selection(market, None, "bookable, but no validated history yet")
            )

    combinations: list[Combination] = []
    priced = [s for s in selections if s.probability is not None]
    for combo in itertools.combinations(priced, min(max_combination_legs, 2)):
        names = tuple(s.market for s in combo)
        naive = 1.0
        for s in combo:
            naive *= s.probability

        # Impossible pairs are worth saying out loud — a builder that cannot win
        # is a different problem from one that is merely unlikely.
        if table.is_impossible(*names):
            combinations.append(Combination(names, naive, 0.0, sample=0))
            continue

        ratio = table.ratio(*names)
        true_p, sample = None, None
        if ratio is not None:
            # Scale the fixture's naive product by the league-wide dependence.
            # Capped at 1.0: a ratio applied to two already-likely outcomes can
            # otherwise produce a probability above certainty.
            true_p = min(naive * ratio, 1.0)
            _, sample = _true_joint(table, names)

        combinations.append(Combination(names, naive, true_p, sample))

    # Most useful first: the combinations where multiplying misleads you most.
    combinations.sort(key=lambda c: abs((c.ratio or 1.0) - 1.0), reverse=True)
    return FixtureSheet(home, away, selections, combinations)


def format_sheet(
    sheets: Iterable[FixtureSheet],
    margin: float = DEFAULT_BUILDER_MARGIN,
    top_combinations: int = 6,
) -> str:
    """Render the sheets as plain text, suitable for Telegram."""
    out: list[str] = []
    sheets = list(sheets)

    out.append(f"BET BUILDER SHEET — {len(sheets)} fixture(s)")
    out.append(f"builder margin assumed: {margin*100:.0f}%")
    out.append("")

    for sheet in sheets:
        out.append(f"── {sheet.label} ──")
        priced = [s for s in sheet.selections if s.probability is not None]
        unpriced = [s for s in sheet.selections if s.probability is None]

        out.append("  selections:")
        for s in priced:
            out.append(f"    {s.market:<16} {s.probability*100:5.1f}%   fair {s.fair_odds:5.2f}")
        for s in unpriced:
            out.append(f"    {s.market:<16}    —     {s.note}")

        out.append("  combinations (where multiplying misleads most):")
        shown = [c for c in sheet.combinations if c.ratio is not None][:top_combinations]
        if not shown:
            out.append("    none with enough history to state")
        for c in shown:
            req = c.required_odds(margin)
            out.append(
                f"    {' + '.join(c.markets):<28} "
                f"naive {c.naive_probability*100:5.1f}%  true {c.true_probability*100:5.1f}%  "
                f"(x{c.ratio:.2f})"
            )
            out.append(f"      {'':<28} fair {c.fair_odds:5.2f} → need better than {req:5.2f}")
            out.append(f"      {'':<28} {c.verdict}")
        out.append("")

    out.append("Every price above is margin-free. A builder keeps 15-25%, so the")
    out.append("book must beat the 'need better than' column before this is worth")
    out.append("staking — and combining builders across fixtures multiplies that")
    out.append("margin again, not the edge.")
    return "\n".join(out)
