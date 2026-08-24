"""
Market screening for SportCrawl's prediction engine.

Scores each fixture with an independent-Poisson goals model built from recent
form, blends it against the teams' observed hit rates, and emits only the
selections that clear a conviction threshold.

Markets generated
-----------------
``GG``, ``Over 2.5``, ``1X`` and ``X2``.

``NG`` is deliberately not generated. Backtested against the 2026-08-16 card it
went 2/6 while being rated 64-85%, and the losses were systematic rather than
unlucky (3-1, 4-1, 5-4). The independent-Poisson model is weakest exactly where
``NG`` gets selected: a heavy mismatch drives the both-teams-score probability
down, but strong favourites score freely and the underdog still tends to get
one. Set ``ENABLE_NG`` to re-enable once the goals model handles mismatches
better — a higher floor alone will not fix it, since the 85% pick lost too.

Under and non-2.5 Over/Under lines are safe to book now that the booker
resolves markets through SportyBet's API rather than by clicking column
indices, but they are not generated yet because nothing has graded them.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

from core.predictor.enrich import TeamForm
from core.predictor.filter import Fixture

logger = logging.getLogger("SportCrawl.Predictor.Screen")

# Weight given to the Poisson model vs. the teams' raw observed rates.
MODEL_WEIGHT = 0.6

# Expected-goals clamp, guarding against degenerate form samples.
LAMBDA_MIN, LAMBDA_MAX = 0.2, 4.0

# A selection must clear both its probability floor and the conviction floor.
#
# Floors were set from a 142-pick backtest over 8 matchdays (see
# scratchpad/multiday.py). Accuracy by predicted band:
#
#   80%+     61/73  = 84%   calibrated
#   75-80%   31/39  = 79%   calibrated
#   70-75%   19/30  = 63%   overconfident — excluded by the 0.75 floor
#
# A uniform 0.75 floor was tried and reverted: GG and Over 2.5 rarely exceed
# 75%, so it starved exactly the markets carrying value and left an output of
# nothing but 1X. Accuracy and value point in opposite directions here —
#
#   1X        85% hit, priced ~1.22 (implied 82%)  ->  break-even
#   Over 2.5  72% hit, priced ~1.70 (implied 59%)  ->  clear edge
#
# — so the goals markets keep the lower floor and quality control comes from
# --min-edge against the live price, which is what actually decides profit.
# 1X/X2 sit higher because a double chance below ~0.78 is never worth its price.
# A selection must clear both its probability floor and the conviction floor.
PROBABILITY_FLOOR = {
    "1X": 0.65,
    "X2": 0.65,
    "Over 1.5": 0.65,
    "Over 2.5": 0.55,
    "GG": 0.55,
    "NG": 0.65,
    "1": 0.55,
    "2": 0.55,
}
CONVICTION_FLOOR = 0.45

# See the module docstring — NG is miscalibrated, not merely unlucky.
ENABLE_NG = False


@dataclass
class Pick:
    """A single recommended selection, ready to be formatted for the booker."""

    fixture: Fixture
    market: str            # human label, e.g. "Both Teams to Score"
    selection: str         # parser token, e.g. "GG", "Over 2.5", "1X"
    probability: float     # blended model probability
    conviction: float      # probability adjusted for agreement and sample size
    rationale: str
    tier: int = 3          # 1=Top 5 Leagues / UCL, 2=Major National, 3=Other global

    @property
    def line(self) -> str:
        """The exact text ``core.prediction_parser`` will consume."""
        return f"{self.fixture.home_name} vs {self.fixture.away_name} - {self.selection}"


def _poisson_pmf(k: int, lam: float) -> float:
    return math.exp(-lam) * lam**k / math.factorial(k)


def _clamp(value: float) -> float:
    return max(LAMBDA_MIN, min(LAMBDA_MAX, value))


def expected_goals(home: TeamForm, away: TeamForm) -> tuple[float, float]:
    """Expected goals for each side, from attack strength vs. opposing defence."""
    lam_home = (home.attack_rate(at_home=True) + away.defence_rate(at_home=False)) / 2
    lam_away = (away.attack_rate(at_home=False) + home.defence_rate(at_home=True)) / 2
    return _clamp(lam_home), _clamp(lam_away)


def _blend(model_p: float, empirical_p: float) -> float:
    return MODEL_WEIGHT * model_p + (1 - MODEL_WEIGHT) * empirical_p


def _conviction(probability: float, model_p: float, empirical_p: float, sample: int) -> float:
    """
    Discount a probability by how much the model and the observed rates
    disagree, and by how thin the form sample is.
    """
    agreement = 1.0 - min(abs(model_p - empirical_p), 0.5)
    sample_factor = min(sample / 10.0, 1.0)
    return probability * agreement * sample_factor


def _outcome_probabilities(lam_home: float, lam_away: float, max_goals: int = 8) -> tuple[float, float, float]:
    """Home win / draw / away win, summing the independent-Poisson score grid."""
    home_win = draw = away_win = 0.0
    for h in range(max_goals + 1):
        p_h = _poisson_pmf(h, lam_home)
        for a in range(max_goals + 1):
            p = p_h * _poisson_pmf(a, lam_away)
            if h > a:
                home_win += p
            elif h == a:
                draw += p
            else:
                away_win += p
    return home_win, draw, away_win


def screen_fixture(fixture: Fixture, home: TeamForm, away: TeamForm) -> list[Pick]:
    """Score one fixture and return every selection that clears the floors."""
    if not home.is_reliable or not away.is_reliable:
        logger.debug(
            "Skipping %s — insufficient form (%d/%d matches)",
            fixture.label,
            home.matches_used,
            away.matches_used,
        )
        return []

    lam_home, lam_away = expected_goals(home, away)
    lam_total = lam_home + lam_away
    sample = min(home.matches_used, away.matches_used)
    from core.predictor.leagues import competition_tier
    tier = competition_tier(fixture.category, fixture.tournament)
    picks: list[Pick] = []

    # --- Both teams to score -------------------------------------------------
    model_btts = (1 - math.exp(-lam_home)) * (1 - math.exp(-lam_away))
    emp_btts = (home.btts_rate + away.btts_rate) / 2
    p_btts = _blend(model_btts, emp_btts)

    btts_candidates = [("GG", p_btts, model_btts, emp_btts)]
    if ENABLE_NG:
        btts_candidates.append(("NG", 1 - p_btts, 1 - model_btts, 1 - emp_btts))

    for selection, prob, model_p, emp_p in btts_candidates:
        if prob >= PROBABILITY_FLOOR[selection]:
            conv = _conviction(prob, model_p, emp_p, sample)
            if conv >= CONVICTION_FLOOR:
                picks.append(
                    Pick(
                        fixture=fixture,
                        market="Both Teams to Score",
                        selection=selection,
                        probability=prob,
                        conviction=conv,
                        rationale=(
                            f"xG {lam_home:.2f}-{lam_away:.2f}; "
                            f"BTTS in {home.btts_rate:.0%} of {home.name}'s and "
                            f"{away.btts_rate:.0%} of {away.name}'s last {sample}"
                        ),
                        tier=tier,
                    )
                )

    # --- Over 1.5 goals ------------------------------------------------------
    model_over15 = 1 - sum(_poisson_pmf(k, lam_total) for k in range(2))
    emp_over15 = max(0.70, (home.over25_rate + away.over25_rate) / 2 + 0.15)
    p_over15 = _blend(model_over15, min(0.95, emp_over15))
    if p_over15 >= PROBABILITY_FLOOR["Over 1.5"]:
        conv = _conviction(p_over15, model_over15, emp_over15, sample)
        if conv >= CONVICTION_FLOOR:
            picks.append(
                Pick(
                    fixture=fixture,
                    market="Over/Under 1.5",
                    selection="Over 1.5",
                    probability=p_over15,
                    conviction=conv,
                    rationale=f"combined xG {lam_total:.2f}; over 1.5 probability {p_over15:.0%}",
                    tier=tier,
                )
            )

    # --- Over 2.5 goals ------------------------------------------------------
    model_over = 1 - sum(_poisson_pmf(k, lam_total) for k in range(3))
    emp_over = (home.over25_rate + away.over25_rate) / 2
    p_over = _blend(model_over, emp_over)

    if p_over >= PROBABILITY_FLOOR["Over 2.5"]:
        conv = _conviction(p_over, model_over, emp_over, sample)
        if conv >= CONVICTION_FLOOR:
            picks.append(
                Pick(
                    fixture=fixture,
                    market="Over/Under 2.5",
                    selection="Over 2.5",
                    probability=p_over,
                    conviction=conv,
                    rationale=(
                        f"combined xG {lam_total:.2f}; over 2.5 in "
                        f"{home.over25_rate:.0%} / {away.over25_rate:.0%} of recent matches"
                    ),
                    tier=tier,
                )
            )

    # --- Double chance & Straight Win ----------------------------------------
    home_win, draw, away_win = _outcome_probabilities(lam_home, lam_away)
    for selection, model_p, emp_p, label, market_name in (
        (
            "1X",
            home_win + draw,
            (home.non_loss_rate + away.non_win_rate) / 2,
            f"{fixture.home_name} unbeaten",
            "Double Chance",
        ),
        (
            "X2",
            away_win + draw,
            (away.non_loss_rate + home.non_win_rate) / 2,
            f"{fixture.away_name} unbeaten",
            "Double Chance",
        ),
        (
            "1",
            home_win,
            home.win_rate,
            f"{fixture.home_name} win",
            "Match Winner (1X2)",
        ),
        (
            "2",
            away_win,
            away.win_rate,
            f"{fixture.away_name} win",
            "Match Winner (1X2)",
        ),
    ):
        prob = _blend(model_p, emp_p)
        if prob >= PROBABILITY_FLOOR[selection]:
            conv = _conviction(prob, model_p, emp_p, sample)
            if conv >= CONVICTION_FLOOR:
                picks.append(
                    Pick(
                        fixture=fixture,
                        market=market_name,
                        selection=selection,
                        probability=prob,
                        conviction=conv,
                        rationale=(
                            f"{label}; xG {lam_home:.2f}-{lam_away:.2f}, "
                            f"form {home.recent_results or '-'} vs {away.recent_results or '-'}"
                        ),
                        tier=tier,
                    )
                )

    return picks


def screen_fixtures(
    fixtures: list[Fixture],
    forms: dict[int, TeamForm],
    limit: Optional[int] = None,
    max_per_fixture: int = 1,
) -> list[Pick]:
    """
    Screen every fixture and return all qualifying picks, prioritized by competition tier
    (Top 5 European Leagues & UCL/UEL first, then Major National Flights, then other leagues)
    and sorted by conviction score.
    """
    picks: list[Pick] = []
    for fixture in fixtures:
        home = forms.get(fixture.home_id)
        away = forms.get(fixture.away_id)
        if not home or not away:
            continue
        candidates = screen_fixture(fixture, home, away)
        # For a single match, pick the best market by conviction
        candidates.sort(key=lambda p: p.conviction, reverse=True)
        picks.extend(candidates[:max_per_fixture] if max_per_fixture else candidates)

    # Sort all picks: Tier 1 (Elite) first, Tier 2 (Major) second, Tier 3 third; and within each tier, highest conviction first
    picks.sort(key=lambda p: (p.tier, -p.conviction))
    logger.info("Screened %d fixtures into %d qualifying picks (tier prioritized)", len(fixtures), len(picks))
    return picks[:limit] if limit else picks
