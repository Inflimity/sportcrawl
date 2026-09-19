"""
Corner, shots-on-target and team-total legs for the Top 20 card.

WHY THESE ARE SEPARATE FROM screen.py
-------------------------------------
`screen.py` holds the seven markets whose hit rate has been measured (Over 1.5,
Over 2.5, GG, 1X, X2, 1, 2). Nothing in this file has been. Keeping them in
different modules means the boundary is visible in the import list rather than
buried in a flag, and means the validated screener cannot accidentally inherit
a floor tuned for an unmeasured market.

They were requested for the Top 20 so that the card stops being twenty copies
of the same Over 1.5 model. That reason is sound on its own terms — twenty legs
of one market is one biased estimate entered twenty times, not a diversified
card (see `tickets.cap_per_market`). The cost is that the new legs carry an
unknown rate against a wider margin, and every one of them is flagged
`validated=False` all the way through to the nightly report.

HOW THE LINE IS CHOSEN — THE §2 TRAP, AVOIDED
---------------------------------------------
The obvious implementation scores every available line and emits the best one.
ANALYSIS.md §2 measured what that does: 210 corner lines had a mean edge of
-0.40%, and taking the best line per fixture reported "+8.0%" on 21 of 21
fixtures. The number was the maximum of the noise, not a read on the match.

So the line here is **chosen by the expected count, before any probability is
computed**, by a fixed rule with no maximisation in it:

    corners     line = the .5 line one full corner below expectation
    shots       line = the .5 line 1.5 below expectation
    team goals  Over 0.5 below 2.2 expected, Over 1.5 above it

The probability is then computed for that one line and checked against a floor.
A fixture contributes a leg or it does not; it never contributes "its best of
six". The floors sit higher than the goals markets' precisely because these
carry 5.8-8.2% margin against goals' 3.8%, so a marginal read is not merely
weaker here, it is negative.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

from core.predictor.enrich import TeamForm
from core.predictor.filter import Fixture
from core.predictor.screen import Pick, expected_goals
from core.predictor.stats_form import (
    StatForm,
    corner_over_probability,
    sot_over_probability,
)

logger = logging.getLogger("SportCrawl.Predictor.ExtraMarkets")

# Higher than the goals floors on purpose. §7 prices corners at 5.8-6.0% and
# shots on target at 8.0%, against 3.8% for match goals: the measured +4.3pp
# form_pick edge is worth +1.6% per leg at 3.8% and goes NEGATIVE at 6%. A leg
# that would be a thin call on goals is a losing call here.
PROBABILITY_FLOOR = {
    "corners": 0.62,
    "shots": 0.64,
    "team_goals": 0.68,
}

# Clamps keep the emitted line inside what a bookmaker actually lists. A line
# outside these is a sign the sample is degenerate, not an opportunity.
CORNER_LINE_RANGE = (7.5, 12.5)
SOT_LINE_RANGE = (5.5, 11.5)

# Cushion between expectation and the line, in whole units. One corner and one
# and a half shots: enough that an average match clears it, small enough that
# the leg still carries a price worth booking.
CORNER_CUSHION = 1.0
SOT_CUSHION = 1.5

# Expected team goals above which the team total steps up from 0.5 to 1.5.
TEAM_GOALS_STEP = 2.2


def _half_line(value: float, lo: float, hi: float) -> float:
    """The .5 line at or below ``value``, clamped into the listed range."""
    line = math.floor(value) + 0.5
    if line > value:
        line -= 1.0
    return max(lo, min(hi, line))


def _conviction(probability: float, sample: int, floor_sample: int = 10) -> float:
    """
    Discount by sample only.

    `screen._conviction` also discounts by model-vs-empirical disagreement,
    which needs two independent estimates of the same quantity. There is only
    one estimate here, so claiming an agreement term would be inventing
    corroboration that does not exist.
    """
    return probability * min(sample / floor_sample, 1.0)


def screen_corners(
    fixture: Fixture, home: StatForm, away: StatForm, tier: int = 3
) -> Optional[Pick]:
    """One corners leg, or None when the sample is too thin to state a rate."""
    lam_pair = corner_over_probability(home, away, 0.5)   # any line; we want lambda
    if lam_pair is None:
        return None
    _, sample, lam = lam_pair

    line = _half_line(lam - CORNER_CUSHION, *CORNER_LINE_RANGE)
    result = corner_over_probability(home, away, line)
    if result is None:
        return None
    prob, sample, lam = result
    if prob < PROBABILITY_FLOOR["corners"]:
        return None

    return Pick(
        fixture=fixture,
        market="Corners Over/Under",
        selection=f"Over {line} Corners",
        probability=prob,
        conviction=_conviction(prob, sample),
        rationale=(
            f"corners {lam:.1f} expected ({home.corners_for:.1f}+{away.corners_for:.1f} "
            f"taken, {sample}-match banked sample) — UNVALIDATED market"
        ),
        tier=tier,
        validated=False,
        sample=sample,
    )


def screen_shots(
    fixture: Fixture, home: StatForm, away: StatForm, tier: int = 3
) -> Optional[Pick]:
    """One shots-on-target leg, or None when the sample is too thin."""
    seed = sot_over_probability(home, away, 0.5)
    if seed is None:
        return None
    _, _, lam = seed

    line = _half_line(lam - SOT_CUSHION, *SOT_LINE_RANGE)
    result = sot_over_probability(home, away, line)
    if result is None:
        return None
    prob, sample, lam = result
    if prob < PROBABILITY_FLOOR["shots"]:
        return None

    return Pick(
        fixture=fixture,
        market="Shots on Target Over/Under",
        selection=f"Over {line} Shots On Target",
        probability=prob,
        conviction=_conviction(prob, sample),
        rationale=(
            f"shots on target {lam:.1f} expected ({sample}-match banked sample) "
            f"— UNVALIDATED market, 8.0% margin"
        ),
        tier=tier,
        validated=False,
        sample=sample,
    )


def screen_team_goals(
    fixture: Fixture, home: TeamForm, away: TeamForm, tier: int = 3
) -> list[Pick]:
    """
    Team-total legs for either side.

    Unlike corners and shots this needs no new data — it is the same Poisson
    already fitted for match goals, read one team at a time. It is still marked
    unvalidated, because a per-team total has never been graded here and sits
    on a wider market than the match total it is derived from.

    WORDING IS LOAD-BEARING HERE
    ----------------------------
    The selection must read "Home Over N" / "Away Over N", not
    "<Team name> Over N". Both look right in a digest and only one books.

    `market_mapper.resolve_by_description` requires EVERY token of the query
    to be explained by the market description, its specifier and the outcome
    name. SportyBet calls these markets "Total - Home" and "Total - Away" and
    never puts the club's name in them, so "Porto Over 0.5" leaves "porto"
    unexplained, matches nothing, and the leg is dropped as unmatched —
    verified against the resolver, not assumed.

    An unqualified "Over 0.5" is worse than dropped: the parser's fast path
    would book it as MATCH goals, so the digest would show a team total and
    the slip would carry a different bet. The "Home"/"Away" qualifier is what
    sends the line to the description resolver instead of the fast path.
    """
    lam_home, lam_away = expected_goals(home, away)
    out: list[Pick] = []

    for side, team_name, lam, other in (
        ("Home", fixture.home_name, lam_home, fixture.away_name),
        ("Away", fixture.away_name, lam_away, fixture.home_name),
    ):
        line = 1.5 if lam >= TEAM_GOALS_STEP else 0.5
        # P(goals > line) for Poisson(lam).
        cdf = 0.0
        term = math.exp(-lam)
        for k in range(0, int(math.floor(line)) + 1):
            cdf += term
            term *= lam / (k + 1)
        prob = max(0.0, min(1.0, 1.0 - cdf))

        if prob < PROBABILITY_FLOOR["team_goals"]:
            continue

        out.append(
            Pick(
                fixture=fixture,
                market=f"{team_name} Total Goals",
                selection=f"{side} Over {line}",
                probability=prob,
                conviction=_conviction(prob, min(home.matches_used, away.matches_used)),
                rationale=(
                    f"{team_name} {lam:.2f} xG vs {other} — UNVALIDATED market"
                ),
                # `market` carries the club name so the digest still reads as
                # a bet on Porto, while `selection` stays in the wording the
                # booker can resolve.
                tier=tier,
                validated=False,
                sample=min(home.matches_used, away.matches_used),
            )
        )

    return out


def screen_extra_markets(
    fixture: Fixture,
    home_form: TeamForm,
    away_form: TeamForm,
    stat_forms: Optional[dict[int, StatForm]] = None,
    tier: int = 3,
    enable_corners: bool = True,
    enable_shots: bool = True,
    enable_team_goals: bool = True,
) -> list[Pick]:
    """Every unvalidated leg this fixture supports, best-first by conviction."""
    picks: list[Pick] = []

    if enable_team_goals:
        picks.extend(screen_team_goals(fixture, home_form, away_form, tier))

    stat_forms = stat_forms or {}
    hs = stat_forms.get(fixture.home_id)
    as_ = stat_forms.get(fixture.away_id)
    if hs and as_:
        if enable_corners:
            leg = screen_corners(fixture, hs, as_, tier)
            if leg:
                picks.append(leg)
        if enable_shots:
            leg = screen_shots(fixture, hs, as_, tier)
            if leg:
                picks.append(leg)

    picks.sort(key=lambda p: p.conviction, reverse=True)
    return picks
