"""
Corner and shot profiles built from BANKED MATCH-LEVEL history.

WHY THIS EXISTS, AND WHAT IT IS NOT
-----------------------------------
ANALYSIS.md §8 found no repeatable corner edge. The reason recorded there is
the part that matters: that work was built on **public season averages**, which
are noisy, measured against average opposition, and — per §6 — frequently hand
you corners TAKEN when you asked for corners CONCEDED.

This module does not repeat that. It reads per-match corner and shot counts
from `/event/{id}/statistics`, the same source we grade goals against, for the
matches already in each team's form window. `for` and `against` are derived
from the same row (the away side's `for` IS the home side's `against`), so the
§6 column swap cannot happen here.

That removes the *excuse*. It does not, on its own, produce an edge, and this
module is deliberate about the difference:

  - a corner line is emitted ONLY when both teams clear MIN_STAT_MATCHES.
  - every probability carries the sample it was computed from, so the caller
    can show it and the nightly report can grade it.
  - §7 prices corners at 5.8-6.0% margin and shots at 8.0%, against 3.8% for
    match goals. The +4.3pp form_pick edge is worth +1.6% per leg at 3.8% and
    turns NEGATIVE at 6%. A corner leg therefore has to be better than the
    goals model by a wide margin just to break even, and nothing here has
    demonstrated that yet.

These legs were requested with that stated. They are marked unvalidated
wherever they surface so the distinction survives into the digest and into the
nightly report, rather than living only in this docstring.

WHY A FILE CACHE AND NOT THE match_stats TABLE
----------------------------------------------
`storage.models.MatchStats` exists and is the right long-term home, but the
prediction pipeline holds no database handle, and opening a second async
SQLAlchemy engine against the same SQLite file while the live service holds one
invites writer lock contention on a box that is already the single point of
failure. This follows the pattern `core/ticket_log.py` and `logs/outcomes.json`
already set: append-only, lock-free, in `logs/`.

A finished match's statistics never change, so a match is fetched once ever and
the sample compounds from the day this is switched on.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger("SportCrawl.Predictor.StatsForm")

DEFAULT_STATS_CACHE = os.getenv("MATCH_STATS_CACHE", "logs/match_stats.jsonl")

# Below this many matches WITH PUBLISHED STATISTICS, a team has no corner or
# shot profile and no line is emitted for it. Ten is the goals window; corners
# are noisier than goals, so accepting fewer here would be claiming more
# precision from less data. Six is the floor at which a mean is worth stating
# at all, and it is still thin — the digest says so.
MIN_STAT_MATCHES = 6

# Corner counts are mildly overdispersed relative to Poisson (variance runs
# ~1.1-1.3x the mean in published samples). Poisson therefore understates the
# tails slightly, which makes an Over line look marginally MORE likely than it
# is. Shrinking lambda toward the pooled mean is the cheap correction: it pulls
# extreme per-team reads back and costs nothing in the common case.
CORNER_SHRINKAGE = 0.25
POOLED_CORNERS_PER_MATCH = 9.8      # league-agnostic anchor, full match, both sides
POOLED_SOT_PER_MATCH = 8.4          # shots on target, both sides


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------


def load_stats_cache(path: Optional[str] = None) -> dict[int, dict[str, Any]]:
    """
    Read the banked statistics. Missing or corrupt lines are skipped, never
    fatal — a cache is an optimisation and must not be able to stop a digest.
    """
    target = Path(path or DEFAULT_STATS_CACHE)
    out: dict[int, dict[str, Any]] = {}
    if not target.exists():
        return out
    try:
        for line in target.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            mid = row.get("match_id")
            if mid is None:
                continue
            out[int(mid)] = row          # later rows win; the file is append-only
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not read stats cache (%s); continuing without it.", e)
    return out


def append_stats_cache(lines: Iterable[Any], path: Optional[str] = None) -> int:
    """Append MatchStatLine objects. Returns rows written; 0 on any failure."""
    rows = []
    for line in lines:
        mid = getattr(line, "match_id", None)
        if mid is None:
            continue
        rows.append({
            "match_id": int(mid),
            "home_corners": getattr(line, "home_corners", None),
            "away_corners": getattr(line, "away_corners", None),
            "home_corners_ht": getattr(line, "home_corners_ht", None),
            "away_corners_ht": getattr(line, "away_corners_ht", None),
            "home_shots_on_target": getattr(line, "home_shots_on_target", None),
            "away_shots_on_target": getattr(line, "away_shots_on_target", None),
            "home_yellow_cards": getattr(line, "home_yellow_cards", None),
            "away_yellow_cards": getattr(line, "away_yellow_cards", None),
            "home_possession": getattr(line, "home_possession", None),
        })
    if not rows:
        return 0
    try:
        target = Path(path or DEFAULT_STATS_CACHE)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        return len(rows)
    except Exception as e:  # noqa: BLE001
        logger.warning("Stats cache write failed (%s); continuing without it.", e)
        return 0


# --------------------------------------------------------------------------
# Per-team profile
# --------------------------------------------------------------------------


@dataclass
class StatForm:
    """
    One team's corner and shot rates over its recent matches WITH statistics.

    ``matches`` is the count of matches that actually published the figure, not
    the size of the form window. A team with ten recent matches of which three
    published corners has ``corner_matches == 3`` and is not bettable.
    """

    team_id: int
    name: str = ""
    corner_matches: int = 0
    corners_for: float = 0.0        # per match, taken by this team
    corners_against: float = 0.0    # per match, taken by the opposition
    sot_matches: int = 0
    sot_for: float = 0.0
    sot_against: float = 0.0

    @property
    def has_corners(self) -> bool:
        return self.corner_matches >= MIN_STAT_MATCHES

    @property
    def has_shots(self) -> bool:
        return self.sot_matches >= MIN_STAT_MATCHES


def build_stat_forms(
    raw_by_team: dict[int, list[dict[str, Any]]],
    stats: dict[int, dict[str, Any]],
    names: Optional[dict[int, str]] = None,
    window: int = 10,
) -> dict[int, StatForm]:
    """
    Turn banked statistics into per-team rates.

    ``raw_by_team`` is the untouched event list ``fetch_team_forms`` already
    produces, so which matches count — and in which order — is decided by the
    same code that decides it for goals. Reading side from the event means a
    team's `corners_for` is always the column for the side it actually played,
    which is the §6 guarantee.
    """
    names = names or {}
    out: dict[int, StatForm] = {}

    for team_id, events in (raw_by_team or {}).items():
        form = StatForm(team_id=int(team_id), name=names.get(int(team_id), ""))
        c_for = c_against = s_for = s_against = 0.0
        used = 0

        for ev in (events or [])[:window]:
            status = ((ev.get("status") or {}).get("type") or "").lower()
            if status and status != "finished":
                continue
            eid = ev.get("id")
            if eid is None:
                continue
            row = stats.get(int(eid))
            if not row:
                continue

            # Which side was this team? Read it off the event, never assumed.
            home_id = ((ev.get("homeTeam") or {}).get("id"))
            is_home = home_id is not None and int(home_id) == int(team_id)
            away_id = ((ev.get("awayTeam") or {}).get("id"))
            if not is_home and not (away_id is not None and int(away_id) == int(team_id)):
                continue

            hc, ac = row.get("home_corners"), row.get("away_corners")
            if hc is not None and ac is not None:
                c_for += hc if is_home else ac
                c_against += ac if is_home else hc
                form.corner_matches += 1

            hs, as_ = row.get("home_shots_on_target"), row.get("away_shots_on_target")
            if hs is not None and as_ is not None:
                s_for += hs if is_home else as_
                s_against += as_ if is_home else hs
                form.sot_matches += 1
            used += 1

        if form.corner_matches:
            form.corners_for = c_for / form.corner_matches
            form.corners_against = c_against / form.corner_matches
        if form.sot_matches:
            form.sot_for = s_for / form.sot_matches
            form.sot_against = s_against / form.sot_matches
        out[int(team_id)] = form

    return out


# --------------------------------------------------------------------------
# Lines
# --------------------------------------------------------------------------


def _poisson_over(line: float, lam: float, max_k: int = 40) -> float:
    """P(count > line) for a Poisson(lam). ``line`` is a .5 line, so no ties."""
    if lam <= 0:
        return 0.0
    threshold = math.floor(line) + 1
    # Sum the tail directly; the head is shorter for the lines we use.
    cdf = 0.0
    term = math.exp(-lam)
    for k in range(0, threshold):
        cdf += term
        term *= lam / (k + 1)
    return max(0.0, min(1.0, 1.0 - cdf))


def _shrink(lam: float, pooled: float, weight: float = CORNER_SHRINKAGE) -> float:
    return (1.0 - weight) * lam + weight * pooled


def expected_corners(home: StatForm, away: StatForm) -> Optional[tuple[float, int]]:
    """
    Expected TOTAL corners, and the smaller of the two samples behind it.

    Same attack-vs-defence shape as ``screen.expected_goals``: what this team
    takes, against what that team concedes, averaged. Returns None when either
    side is below the sample floor — an unknown rate must never silently become
    a league average dressed up as a read.
    """
    if not (home.has_corners and away.has_corners):
        return None
    lam_home = (home.corners_for + away.corners_against) / 2
    lam_away = (away.corners_for + home.corners_against) / 2
    total = _shrink(lam_home + lam_away, POOLED_CORNERS_PER_MATCH)
    return total, min(home.corner_matches, away.corner_matches)


def expected_sot(home: StatForm, away: StatForm) -> Optional[tuple[float, int]]:
    """Expected total shots on target, and the smaller sample behind it."""
    if not (home.has_shots and away.has_shots):
        return None
    lam_home = (home.sot_for + away.sot_against) / 2
    lam_away = (away.sot_for + home.sot_against) / 2
    total = _shrink(lam_home + lam_away, POOLED_SOT_PER_MATCH)
    return total, min(home.sot_matches, away.sot_matches)


def corner_over_probability(home: StatForm, away: StatForm, line: float) -> Optional[tuple[float, int, float]]:
    """P(total corners > line), the sample, and the lambda used. None if thin."""
    exp = expected_corners(home, away)
    if exp is None:
        return None
    lam, sample = exp
    return _poisson_over(line, lam), sample, lam


def sot_over_probability(home: StatForm, away: StatForm, line: float) -> Optional[tuple[float, int, float]]:
    """P(total shots on target > line), the sample, and the lambda used."""
    exp = expected_sot(home, away)
    if exp is None:
        return None
    lam, sample = exp
    return _poisson_over(line, lam), sample, lam


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


async def ensure_stats(
    raw_by_team: dict[int, list[dict[str, Any]]],
    window: int = 10,
    max_fetch: int = 300,
    cache_path: Optional[str] = None,
    settings: Optional[Any] = None,
) -> dict[int, dict[str, Any]]:
    """
    Return banked statistics for the form matches, fetching what is missing.

    Cost control matters here: a 50-fixture card is 100 teams, and ten matches
    each is a thousand requests against a source that throttles. Three limits
    keep it bounded, and they compound — after the first few days almost
    everything is a cache hit and this costs nothing.

    1. **A finished match is fetched once, ever.** Statistics do not change.
    2. **Deduplicated across teams.** Both participants list the same match; it
       is one id, so it is one request.
    3. **Capped at ``max_fetch`` per run**, oldest-first so the backlog drains
       deterministically rather than re-attempting a different random slice
       each night. Exceeding the cap is normal on day one and self-corrects.

    Never raises. A statistics outage must degrade to "no corner legs today",
    not to a failed digest.
    """
    cache = load_stats_cache(cache_path)

    wanted: list[int] = []
    seen: set[int] = set()
    for events in (raw_by_team or {}).values():
        for ev in (events or [])[:window]:
            status = ((ev.get("status") or {}).get("type") or "").lower()
            if status and status != "finished":
                continue
            eid = ev.get("id")
            if eid is None:
                continue
            eid = int(eid)
            if eid in cache or eid in seen:
                continue
            seen.add(eid)
            wanted.append(eid)

    if not wanted:
        logger.info("Match statistics: %d matches already banked, nothing to fetch.", len(cache))
        return cache

    truncated = len(wanted) > max_fetch
    wanted = sorted(wanted)[:max_fetch]
    logger.info(
        "Match statistics: %d banked, fetching %d new%s.",
        len(cache), len(wanted), " (capped this run)" if truncated else "",
    )

    try:
        from core.predictor.match_stats import fetch_match_stats

        fetched = await fetch_match_stats(wanted, settings=settings)
    except Exception as e:  # noqa: BLE001 - see docstring
        logger.warning("Match statistics fetch failed (%s); corner legs will be skipped.", e)
        return cache

    written = append_stats_cache(fetched.values(), cache_path)
    logger.info("Match statistics: banked %d new rows (%d requested).", written, len(wanted))

    for mid, line in fetched.items():
        cache[int(mid)] = {
            "match_id": int(mid),
            "home_corners": line.home_corners,
            "away_corners": line.away_corners,
            "home_corners_ht": line.home_corners_ht,
            "away_corners_ht": line.away_corners_ht,
            "home_shots_on_target": line.home_shots_on_target,
            "away_shots_on_target": line.away_shots_on_target,
            "home_yellow_cards": line.home_yellow_cards,
            "away_yellow_cards": line.away_yellow_cards,
            "home_possession": line.home_possession,
        }
    return cache
