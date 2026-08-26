"""
Draw screening for SportCrawl — a parallel track to ``screen.py``.

This module exists because the main screener cannot price draws. It scores
markets with an *independent* Poisson model, and independent Poisson
systematically under-predicts draws: real football has negative dependence at
low scorelines, so 0-0 and 1-1 occur more often than independence implies.

Dixon-Coles (1997) corrects exactly this with a ``tau`` adjustment on the four
lowest cells of the score grid. The size of the correction is the whole reason
this track can exist at all::

    lam_home = lam_away = 1.25    independent 27.0%  ->  Dixon-Coles 30.3%
    lam_home = lam_away = 1.05    independent 29.9%  ->  Dixon-Coles 33.5%

Break-even on a draw priced at 3.20 is 31.25%. The correction is therefore not
a refinement — it is the difference between a losing bet and a marginal one.

Deliberately separate from ``screen.py``
----------------------------------------
Nothing here feeds the existing floors, calibration or ledger. The main
screener's ``PROBABILITY_FLOOR`` bottoms out at 0.50 and a draw tops out around
0.33, so every draw pick would be rejected before it was ever priced. The draw
track carries its own floor, its own model weight and its own ledger, and the
142-pick backtest that tuned the main engine is not evidence about this one.

What is *not* separate: the output contract. ``screen_draws`` returns the same
``Pick`` objects the main path uses, with ``selection="Draw"``, so odds
attachment, formatting and booking all work unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from core.predictor.enrich import TeamForm
from core.predictor.filter import Fixture
from core.predictor.screen import Pick, _poisson_pmf, expected_goals

logger = logging.getLogger("SportCrawl.Predictor.Draws")

# Dixon-Coles low-score dependence parameter. Negative inflates 0-0 and 1-1 and
# deflates 1-0 and 0-1, which is the observed direction in football.
#
# -0.13 is the conventional starting value from the literature, NOT a figure
# fitted on this repo's data. Fit it before trusting the output: the whole edge
# claimed above is 3-4 percentage points, and rho is what produces it. Fitting
# is a one-dimensional maximum-likelihood search over graded historical
# fixtures — see tools/fit_rho.py.
DEFAULT_RHO = -0.13

# tau stays positive only while rho > -1/max(lam_home, lam_away). With
# LAMBDA_MAX at 4.0 the binding limit is -0.25, so the default sits inside the
# valid region, but a fitted value must still be clamped.
RHO_MIN, RHO_MAX = -0.24, 0.24

# Weight on the Poisson model vs. the teams' observed draw rates.
#
# Higher than screen.py's 0.60 on purpose. A team's recent draw count is a poor
# predictor of its next draw — drawing is not a stable team skill, it is what
# happens when two evenly-matched low-scoring sides meet. The model sees that
# structure through lam; the empirical rate mostly sees noise, so it gets a
# minority vote and is kept only as a sanity check against a broken lam.
DRAW_MODEL_WEIGHT = 0.75

# Hard limit on how far the empirical rate may move the model's figure.
#
# Without this the blend is unbounded upward, and that breaks the strategy in a
# specific way: draws top out near 33.5% under any realistic lambda, so a pick
# showing 40% is not a better bet, it is the empirical term reporting noise. Two
# teams that each drew 4 of their last 10 by chance would price at ~32.5% on a
# model that says 30%, and the 2.5-point difference would be booked as edge.
#
# The cap keeps the observed rate in the sanity-check role it is meant for: it
# can nudge a probability, it cannot manufacture one. This is the same shape of
# bug as the old `max(0.70, ...)` Over 1.5 floor — an unconstrained term that
# only ever fired where it was wrong.
DRAW_EMPIRICAL_CAP = 0.03

# A draw must clear this to be considered. Draws top out near 0.33, so this
# floor is doing far less work than the main screener's — most of the quality
# control here comes from --min-edge against the live price, because at these
# probabilities the price is the only thing that decides profit.
DRAW_PROBABILITY_FLOOR = 0.28
DRAW_CONVICTION_FLOOR = 0.24

# Per-competition draw priors, applied as a shift on the blended probability.
#
# Intentionally empty. Draw rates genuinely do vary by league, but this repo has
# never graded a draw, so every number that could go here would be imported from
# memory rather than measured. That is the exact failure mode that produced the
# `max(0.70, ...)` Over 1.5 floor: a plausible constant that was only ever wrong
# where it fired. Fill this from the backtest's per-competition breakdown once
# there is one, and not before.
LEAGUE_DRAW_PRIOR: dict[str, float] = {}


def dc_tau(home_goals: int, away_goals: int, lam_home: float, lam_away: float, rho: float) -> float:
    """
    Dixon-Coles dependence correction for one cell of the score grid.

    Returns a multiplier on the independent-Poisson probability. Only the four
    lowest scorelines are adjusted; everything else is left alone.
    """
    if home_goals == 0 and away_goals == 0:
        return 1.0 - lam_home * lam_away * rho
    if home_goals == 0 and away_goals == 1:
        return 1.0 + lam_home * rho
    if home_goals == 1 and away_goals == 0:
        return 1.0 + lam_away * rho
    if home_goals == 1 and away_goals == 1:
        return 1.0 - rho
    return 1.0


def score_matrix(
    lam_home: float,
    lam_away: float,
    rho: float = DEFAULT_RHO,
    max_goals: int = 8,
) -> list[list[float]]:
    """
    The Dixon-Coles corrected score grid, renormalised to sum to 1.

    Renormalisation matters twice over: ``tau`` only approximately preserves
    total mass, and truncating at ``max_goals`` discards a little more.
    """
    rho = max(RHO_MIN, min(RHO_MAX, rho))
    grid = [[0.0] * (max_goals + 1) for _ in range(max_goals + 1)]
    total = 0.0

    for h in range(max_goals + 1):
        p_h = _poisson_pmf(h, lam_home)
        for a in range(max_goals + 1):
            p = p_h * _poisson_pmf(a, lam_away) * dc_tau(h, a, lam_home, lam_away, rho)
            p = max(p, 0.0)
            grid[h][a] = p
            total += p

    if total > 0:
        for h in range(max_goals + 1):
            for a in range(max_goals + 1):
                grid[h][a] /= total
    return grid


def draw_probability(lam_home: float, lam_away: float, rho: float = DEFAULT_RHO) -> float:
    """Probability of a level scoreline — the trace of the corrected grid."""
    grid = score_matrix(lam_home, lam_away, rho)
    return sum(grid[k][k] for k in range(len(grid)))


def draw_probability_independent(lam_home: float, lam_away: float, max_goals: int = 8) -> float:
    """
    Uncorrected draw probability, kept so the correction stays measurable.

    Renormalised over the same truncated grid ``score_matrix`` uses. Without
    that the two are not comparable: the corrected figure is renormalised and
    this one would not be, so the difference between them would include the
    truncated tail as well as the correction. The tail is tiny at realistic
    lambdas, but this difference is the entire quantity the draw track claims
    as its edge, and it should not be measured against a shifted baseline.
    """
    grid_total = 0.0
    diagonal = 0.0
    for h in range(max_goals + 1):
        p_h = _poisson_pmf(h, lam_home)
        for a in range(max_goals + 1):
            p = p_h * _poisson_pmf(a, lam_away)
            grid_total += p
            if h == a:
                diagonal += p
    return diagonal / grid_total if grid_total > 0 else 0.0


def _empirical_draw_rate(form: TeamForm) -> float:
    """Share of a team's recent matches that ended level."""
    if not form.recent_results:
        return 0.0
    return sum(r == "D" for r in form.recent_results) / len(form.recent_results)


def screen_draw(
    fixture: Fixture,
    home: TeamForm,
    away: TeamForm,
    rho: float = DEFAULT_RHO,
) -> Optional[Pick]:
    """
    Score one fixture as a draw candidate.

    Returns ``None`` unless the fixture clears both draw floors. Draw
    probability is driven by two things and only two: total expected goals
    (lower is better) and the gap between the two sides (smaller is better).
    The sweet spot is roughly ``lam_total`` 2.1-2.4 with ``lam_diff`` under
    0.25; outside it the probability decays quickly.
    """
    if not home.is_reliable or not away.is_reliable:
        return None

    lam_home, lam_away = expected_goals(home, away)
    lam_total = lam_home + lam_away
    lam_diff = abs(lam_home - lam_away)
    sample = min(home.matches_used, away.matches_used)

    model_p = draw_probability(lam_home, lam_away, rho)
    independent_p = draw_probability_independent(lam_home, lam_away)
    emp_p = (_empirical_draw_rate(home) + _empirical_draw_rate(away)) / 2

    blended = DRAW_MODEL_WEIGHT * model_p + (1 - DRAW_MODEL_WEIGHT) * emp_p
    # The observed rate advises; it does not override. See DRAW_EMPIRICAL_CAP.
    prob = max(model_p - DRAW_EMPIRICAL_CAP, min(model_p + DRAW_EMPIRICAL_CAP, blended))
    prob += LEAGUE_DRAW_PRIOR.get(fixture.tournament, 0.0)
    prob = max(0.0, min(1.0, prob))

    if prob < DRAW_PROBABILITY_FLOOR:
        return None

    # Same discount shape as the main screener, at half the strength on the
    # agreement term.
    #
    # The main screener can take disagreement at face value because its
    # empirical rates measure something stable — how often these teams' games
    # go over 2.5, say. A team's draw count does not: at a 30% draw rate, 10
    # matches yields anywhere from 1 to 5 draws routinely, so a wide gap
    # between model and form is the expected case, not a warning.
    #
    # At full strength this term contradicted DRAW_EMPIRICAL_CAP outright —
    # the cap holds the observed rate to +/-3 points on the probability
    # precisely because it is noise, and then the same number was cutting
    # conviction by half and vetoing the pick. Two teams on a run of draws
    # scored 0.195 against a 0.24 floor and were rejected for being *too*
    # drawish. Halved, it discounts without vetoing.
    agreement = 1.0 - 0.5 * min(abs(model_p - emp_p), 0.5)
    sample_factor = min(sample / 10.0, 1.0)
    conviction = prob * agreement * sample_factor

    if conviction < DRAW_CONVICTION_FLOOR:
        return None

    from core.predictor.leagues import competition_tier

    return Pick(
        fixture=fixture,
        market="Match Winner (1X2)",
        selection="Draw",
        probability=prob,
        conviction=conviction,
        rationale=(
            f"xG {lam_home:.2f}-{lam_away:.2f} (total {lam_total:.2f}, gap {lam_diff:.2f}); "
            f"Dixon-Coles {model_p:.1%} vs independent {independent_p:.1%}; "
            f"drew {_empirical_draw_rate(home):.0%} / {_empirical_draw_rate(away):.0%} of last {sample}"
        ),
        tier=competition_tier(fixture.category, fixture.tournament),
    )


@dataclass
class DrawScreenStats:
    """Where the floors bit, so a thin card can be diagnosed rather than guessed at."""

    considered: int = 0
    no_form: int = 0
    unreliable_form: int = 0
    below_probability_floor: int = 0
    below_conviction_floor: int = 0
    passed: int = 0
    best_rejected_probability: float = 0.0

    def summary(self) -> str:
        return (
            f"Draw screen: {self.considered} fixtures -> {self.passed} candidates "
            f"(no form {self.no_form}, thin form {self.unreliable_form}, "
            f"below {DRAW_PROBABILITY_FLOOR:.0%} probability {self.below_probability_floor}, "
            f"below {DRAW_CONVICTION_FLOOR:.0%} conviction {self.below_conviction_floor}; "
            f"best rejected {self.best_rejected_probability:.1%})"
        )


def screen_draws(
    fixtures: list[Fixture],
    forms: dict[int, TeamForm],
    limit: Optional[int] = None,
    rho: float = DEFAULT_RHO,
    stats: Optional[DrawScreenStats] = None,
) -> list[Pick]:
    """
    Screen every fixture for draw value, best first.

    Ranked by conviction rather than tier. The main screener puts the top
    European leagues first because that is where its markets are liquid, but
    the draw edge — if there is one — is likelier in competitions SportyBet
    prices loosely, so tier ordering would sort against the strategy.

    Pass ``stats`` to find out why a card came back thin. The floors here have
    never been tested against a real fixture list, and ten picks a day is a
    demanding target for a market that only clears 28% on evenly-matched
    low-scoring games — expect to tune them on the first few live runs.
    """
    stats = stats if stats is not None else DrawScreenStats()
    picks: list[Pick] = []

    for fixture in fixtures:
        stats.considered += 1
        home = forms.get(fixture.home_id)
        away = forms.get(fixture.away_id)
        if not home or not away:
            stats.no_form += 1
            continue
        if not home.is_reliable or not away.is_reliable:
            stats.unreliable_form += 1
            continue

        pick = screen_draw(fixture, home, away, rho=rho)
        if pick:
            stats.passed += 1
            picks.append(pick)
            continue

        # Rejected on one of the floors — work out which, for the diagnostic.
        lam_home, lam_away = expected_goals(home, away)
        model_p = draw_probability(lam_home, lam_away, rho)
        emp_p = (_empirical_draw_rate(home) + _empirical_draw_rate(away)) / 2
        blended = DRAW_MODEL_WEIGHT * model_p + (1 - DRAW_MODEL_WEIGHT) * emp_p
        prob = max(model_p - DRAW_EMPIRICAL_CAP, min(model_p + DRAW_EMPIRICAL_CAP, blended))
        stats.best_rejected_probability = max(stats.best_rejected_probability, prob)
        if prob < DRAW_PROBABILITY_FLOOR:
            stats.below_probability_floor += 1
        else:
            stats.below_conviction_floor += 1

    picks.sort(key=lambda p: p.conviction, reverse=True)
    logger.info(stats.summary())
    return picks[:limit] if limit else picks
