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
    "Over 1.5": 0.75,
    "Over 2.5": 0.52,
}

MARKET_LABELS: dict[str, str] = {
    "1": "Match Winner",
    "2": "Match Winner",
    "X": "Match Winner",
    "GG": "Both Teams to Score",
    "Over 1.5": "Total Goals",
    "Over 2.5": "Total Goals",
}

# Preference order, used as the tie-break in ``best_market``. Goal markets come
# first because they need only the pattern to repeat, not a particular side to
# win, and the shorter line comes before the longer one within them.
DEFAULT_MARKETS: list[str] = ["Over 1.5", "GG", "Over 2.5", "1", "2", "X"]


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
        "Over 1.5": ((home.over15_rate + away.over15_rate) / 2, n),
        "Over 2.5": ((home.over25_rate + away.over25_rate) / 2, n),
        "1":        ((home.win_rate + away.loss_rate) / 2, n),
        "2":        ((away.win_rate + home.loss_rate) / 2, n),
        "X":        ((home.draw_rate + away.draw_rate) / 2, n),
    }


def ranked_markets(
    home: TeamForm,
    away: TeamForm,
    markets: Optional[list[str]] = None,
) -> list[tuple[str, float, float, int]]:
    """
    Every candidate market as ``(selection, shrunk_probability, raw_rate, sample)``,
    most-supported first.

    ``Over 1.5`` will usually top this list, because it is a superset of
    ``Over 2.5`` and so cannot be less likely. That is not a reason to leave it
    out — it is the strongest read the form supports, and it is the honest
    answer when the question is "what is safest here". Which of these actually
    goes on a ticket is a question about price as well as probability, and that
    belongs to the ticket builder, which is why the whole ranked list is
    returned rather than just the head of it.
    """
    rates = market_rates(home, away)
    candidates = markets or DEFAULT_MARKETS
    scored = [
        (sel, _shrink(rates[sel][0], rates[sel][1], sel), rates[sel][0], rates[sel][1])
        for sel in candidates
        if sel in rates
    ]
    # Stable sort on the shrunk probability alone, so equal reads keep the
    # preference order the candidate list encodes.
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored


def best_market(
    home: TeamForm,
    away: TeamForm,
    markets: Optional[list[str]] = None,
) -> Optional[tuple[str, float, float, int]]:
    """Return the single best-supported market, or ``None`` if there is none."""
    ranked = ranked_markets(home, away, markets=markets)
    return ranked[0] if ranked else None


def _build_pick(
    fixture: Fixture,
    home: TeamForm,
    away: TeamForm,
    entry: tuple[str, float, float, int],
) -> "Pick":
    """Turn one scored market into the Pick the booker and pricer consume."""
    from core.predictor.screen import Pick

    selection, probability, raw, sample = entry
    return Pick(
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
    )


def _readable_fixtures(
    fixtures: list[Fixture],
    forms: dict[int, TeamForm],
    prefer_short: bool,
    min_matches: int,
) -> tuple[list[tuple[Fixture, TeamForm, TeamForm]], int]:
    """The fixtures with enough form on both sides to be read, and how many were not."""
    readable: list[tuple[Fixture, TeamForm, TeamForm]] = []
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
        readable.append((fixture, home, away))

    return readable, skipped


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
    readable, skipped = _readable_fixtures(fixtures, forms, prefer_short, min_matches)

    scored: list[tuple[float, "Pick"]] = []
    for fixture, home, away in readable:
        ranked = ranked_markets(home, away, markets=markets)
        if not ranked:
            continue
        scored.append((ranked[0][1], _build_pick(fixture, home, away, ranked[0])))

    scored.sort(key=lambda item: item[0], reverse=True)
    logger.info(
        "Form screen: %d picks from %d fixtures (%d skipped for thin form).",
        len(scored), len(fixtures), skipped,
    )
    ranked_picks = [pick for _, pick in scored]
    return ranked_picks[:limit] if limit else ranked_picks


def screen_form_candidates(
    fixtures: list[Fixture],
    forms: dict[int, TeamForm],
    prefer_short: bool = True,
    min_matches: int = DEFAULT_MIN_MATCHES,
    per_fixture: int = 3,
    fixture_limit: Optional[int] = None,
    markets: Optional[list[str]] = None,
) -> list["Pick"]:
    """
    Several selections per fixture, for a ticket builder that also sees price.

    ``screen_form`` commits to one market per fixture on probability alone.
    That is the wrong call when the ticket has an odds cap: the safest market
    is not always the one that fits, and a builder handed a single selection
    per fixture has to drop the whole fixture when its price is too long. Here
    each fixture offers its top ``per_fixture`` markets and the builder chooses
    between them, so a fixture can still contribute at a shorter line.

    Fixtures are ranked by their *best* market and the strongest
    ``fixture_limit`` are kept, because pricing costs one SportyBet event call
    per fixture and the cap is only ever filled from the top of the list.
    Selections within a fixture stay mutually exclusive downstream — the
    builder takes at most one leg per match.
    """
    readable, skipped = _readable_fixtures(fixtures, forms, prefer_short, min_matches)

    per_fixture = max(1, per_fixture)
    by_fixture: list[tuple[float, list["Pick"]]] = []

    for fixture, home, away in readable:
        ranked = ranked_markets(home, away, markets=markets)
        if not ranked:
            continue
        picks = [_build_pick(fixture, home, away, entry) for entry in ranked[:per_fixture]]
        by_fixture.append((ranked[0][1], picks))

    by_fixture.sort(key=lambda item: item[0], reverse=True)
    if fixture_limit:
        by_fixture = by_fixture[:fixture_limit]

    candidates = [pick for _, picks in by_fixture for pick in picks]
    logger.info(
        "Form screen: %d candidates across %d fixtures, up to %d each "
        "(%d skipped for thin form).",
        len(candidates), len(by_fixture), per_fixture, skipped,
    )
    return candidates
