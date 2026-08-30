"""
Form-guide screening: read both teams' last few games and take the best market.

This is the hand method — look at each side's recent results, and judge which
of Home / Away / Draw / GG / Over 2.5 those two form lines point at. It is
deliberately not a model. ``screen.py`` projects goals with Poisson and is the
better instrument for that job; this reads the numbers off the form guide the
way a person does, because that is the process being reproduced.

It costs no extra requests. ``fetch_team_forms(..., short_window=5)`` builds
the five-match cut from the same events it already fetched for the ten-match
one, so both windows come out of a single pass.

Two things it does not do naively:

**It does not treat five games as five games.** A team with 5/5 BTTS has a rate
of 1.0 and that is not a real 100%. Every rate is shrunk toward the league base
rate with a strength of ``PRIOR_STRENGTH`` pseudo-matches, so a short window
cannot outrank a longer one on a hot streak alone.

**It does not read a win rate as a home-win probability.** A home win needs the
home side to win AND the away side to lose, so the two form lines are combined
rather than one being taken alone.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from core.predictor.enrich import TeamForm
from core.predictor.filter import Fixture

if TYPE_CHECKING:
    from core.predictor.screen import Pick

logger = logging.getLogger(__name__)

# Fewest matches of form on BOTH sides before a fixture can be picked.
DEFAULT_MIN_MATCHES = 4

# Prior weight in pseudo-matches. At 3, a five-match window is roughly
# five-eighths observation and three-eighths base rate.
PRIOR_STRENGTH = 3.0

# League-average base rates, the shrink target.
BASE_RATES: dict[str, float] = {
    "1": 0.44,
    "2": 0.29,
    "X": 0.27,
    "GG": 0.52,
    "Over 2.5": 0.52,
}

MARKET_LABELS: dict[str, str] = {
    "1": "Match Winner",
    "2": "Match Winner",
    "X": "Match Winner",
    "GG": "Both Teams to Score",
    "Over 2.5": "Total Goals",
}


def _shrink(rate: float, sample: int, selection: str,
            prior_strength: float = PRIOR_STRENGTH) -> float:
    """Pull an observed rate toward the base rate by how little was observed."""
    if sample <= 0:
        return BASE_RATES[selection]
    prior = BASE_RATES[selection]
    return (rate * sample + prior_strength * prior) / (sample + prior_strength)


def _window(form: Optional[TeamForm], prefer_short: bool) -> Optional[TeamForm]:
    """The short cut if it was built and has data, else the full form."""
    if form is None:
        return None
    if prefer_short and form.short and form.short.matches_used:
        return form.short
    return form if form.matches_used else None


def market_rates(home: TeamForm, away: TeamForm) -> dict[str, tuple[float, int]]:
    """
    Raw rate and effective sample for each market, from the two form lines.

    Result markets combine both sides — a home win needs one team to win and
    the other to lose, so taking the home side's win rate alone would ignore
    half the evidence and read a good team facing a better one as a banker.
    """
    n = min(home.matches_used, away.matches_used)
    return {
        "GG":       ((home.btts_rate + away.btts_rate) / 2, n),
        "Over 2.5": ((home.over25_rate + away.over25_rate) / 2, n),
        "1":        ((home.win_rate + away.loss_rate) / 2, n),
        "2":        ((away.win_rate + home.loss_rate) / 2, n),
        "X":        ((home.draw_rate + away.draw_rate) / 2, n),
    }


def best_market(
    home: TeamForm,
    away: TeamForm,
    markets: Optional[list[str]] = None,
) -> Optional[tuple[str, float, float, int]]:
    """
    Return ``(selection, shrunk_probability, raw_rate, sample)``.

    Candidate order is the tie-break and is therefore a preference order, not
    an accident: goal markets come first because they need only the pattern to
    repeat, not a particular side to win.
    """
    rates = market_rates(home, away)
    candidates = markets or ["GG", "Over 2.5", "1", "2", "X"]
    scored = [
        (sel, _shrink(rates[sel][0], rates[sel][1], sel), rates[sel][0], rates[sel][1])
        for sel in candidates
    ]
    return max(scored, key=lambda item: item[1]) if scored else None


def screen_form(
    fixtures: list[Fixture],
    forms: dict[int, TeamForm],
    prefer_short: bool = True,
    min_matches: int = DEFAULT_MIN_MATCHES,
    limit: Optional[int] = None,
    markets: Optional[list[str]] = None,
) -> list["Pick"]:
    """
    One pick per fixture, best-supported first.

    Fixtures where either side has fewer than ``min_matches`` of form produce
    nothing: with a five-match window the shrink would otherwise be doing most
    of the talking, and a prior dressed as a read is the thing to avoid.
    """
    from core.predictor.screen import Pick

    scored: list[tuple[float, "Pick"]] = []
    skipped = 0

    for fixture in fixtures:
        home = _window(forms.get(fixture.home_id), prefer_short)
        away = _window(forms.get(fixture.away_id), prefer_short)
        if not home or not away:
            skipped += 1
            continue
        if home.matches_used < min_matches or away.matches_used < min_matches:
            skipped += 1
            continue

        chosen = best_market(home, away, markets=markets)
        if not chosen:
            continue
        selection, probability, raw, sample = chosen

        scored.append((
            probability,
            Pick(
                fixture=fixture,
                market=MARKET_LABELS[selection],
                selection=selection,
                probability=probability,
                # Conviction is the unshrunk read: how hard the form itself
                # points this way, before the sample-size discount.
                conviction=raw,
                rationale=(
                    f"last {sample}: {fixture.home_name} {home.recent_results} / "
                    f"{fixture.away_name} {away.recent_results} — "
                    f"{raw:.0%} raw, {probability:.0%} after shrink"
                ),
            ),
        ))

    scored.sort(key=lambda item: item[0], reverse=True)
    logger.info(
        "Form screen: %d picks from %d fixtures (%d skipped for thin form).",
        len(scored), len(fixtures), skipped,
    )
    ranked = [pick for _, pick in scored]
    return ranked[:limit] if limit else ranked
