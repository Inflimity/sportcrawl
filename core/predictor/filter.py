"""
Fixture filtering for SportCrawl's prediction engine.

Reduces a raw SofaScore dump (typically 150-250 fixtures spanning every
competition on earth) to the handful that are worth modelling.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from core.predictor.leagues import is_allowed, is_excluded

logger = logging.getLogger("SportCrawl.Predictor.Filter")

# How close to kickoff a fixture may be and still be worth picking.
#
# ``status_type`` is not enough on its own. It is whatever the last scrape
# wrote, and the upcoming-fixtures sweep does not revisit a match once it has
# kicked off, so a 15:00 game is still stored as "notstarted" at 21:00 and
# sails through the status check. Checking the kickoff timestamp against the
# clock is the check that cannot go stale.
#
# The lead time is not zero because a selection still has to be priced and
# booked after it is screened, and SportyBet closes the market at kickoff.
MIN_LEAD_MINUTES = 5


def kickoff_utc(match: dict[str, Any]) -> Optional[datetime]:
    """
    Kickoff as an aware UTC datetime, or ``None`` if the fixture carries none.

    ``startTimestamp`` is preferred: it is unix epoch seconds and therefore
    unambiguous. ``start_time_utc`` is the fallback and may be naive, in which
    case it is read as UTC — which is what the field name promises.
    """
    ts = match.get("startTimestamp")
    if ts:
        try:
            return datetime.fromtimestamp(int(ts), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            pass

    raw = match.get("start_time_utc")
    if raw:
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    return None


@dataclass
class Fixture:
    """A fixture that survived filtering, flattened for downstream use."""

    match_id: int
    tournament: str
    category: str
    home_name: str
    away_name: str
    home_id: int
    away_id: int
    start_utc: str
    start_local: str

    @property
    def label(self) -> str:
        return f"{self.home_name} vs {self.away_name}"


@dataclass
class FilterStats:
    """Counts of what was dropped and why, for reporting."""

    total: int = 0
    kept: int = 0
    already_started: int = 0
    past_kickoff: int = 0
    excluded_competition: int = 0
    not_allowlisted: int = 0
    malformed: int = 0
    dropped_examples: dict[str, list[str]] = field(default_factory=dict)

    def _note(self, reason: str, label: str) -> None:
        examples = self.dropped_examples.setdefault(reason, [])
        if len(examples) < 5:
            examples.append(label)


def load_fixture_file(path: str | Path) -> list[dict[str, Any]]:
    """Read a sportcrawl_upcoming_*.json dump and return its raw match list."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    return data.get("matches", [])


def filter_fixtures(
    raw_matches: list[dict[str, Any]],
    allow_unlisted: bool = False,
    now: Optional[datetime] = None,
    min_lead_minutes: int = MIN_LEAD_MINUTES,
) -> tuple[list[Fixture], FilterStats]:
    """
    Keep only fixtures worth predicting on.

    Set ``allow_unlisted`` to bypass the competition allowlist while still
    applying the hard exclusions — useful for exploring coverage, not for
    generating real picks.

    Fixtures are dropped once the clock has passed ``min_lead_minutes`` before
    kickoff, regardless of what ``status_type`` claims. Pass ``now`` to filter
    as of another moment — backtests must, or they will screen fixtures the
    graded matchday had already played.
    """
    stats = FilterStats(total=len(raw_matches))
    kept: list[Fixture] = []
    cutoff = (now or datetime.now(timezone.utc)) + timedelta(minutes=min_lead_minutes)
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)

    for match in raw_matches:
        home = match.get("home_team") or {}
        away = match.get("away_team") or {}
        tournament = match.get("tournament") or ""
        category = match.get("category") or ""
        label = f"{home.get('name')} vs {away.get('name')} ({category} / {tournament})"

        if not home.get("name") or not away.get("name") or not match.get("match_id"):
            stats.malformed += 1
            stats._note("malformed", label)
            continue

        if match.get("status_type") != "notstarted":
            stats.already_started += 1
            stats._note("already_started", label)
            continue

        kickoff = kickoff_utc(match)
        if kickoff is not None and kickoff <= cutoff:
            stats.past_kickoff += 1
            stats._note("past_kickoff", f"{label} @ {kickoff.isoformat()}")
            continue

        if is_excluded(tournament):
            stats.excluded_competition += 1
            stats._note("excluded_competition", label)
            continue

        if not allow_unlisted and not is_allowed(category, tournament):
            stats.not_allowlisted += 1
            stats._note("not_allowlisted", label)
            continue

        kept.append(
            Fixture(
                match_id=int(match["match_id"]),
                tournament=tournament,
                category=category,
                home_name=home["name"],
                away_name=away["name"],
                home_id=int(home.get("id") or 0),
                away_id=int(away.get("id") or 0),
                start_utc=match.get("start_time_utc") or "",
                start_local=match.get("start_time_wat") or "",
            )
        )

    stats.kept = len(kept)
    logger.info(
        "Filtered %d fixtures down to %d tradeable "
        "(%d not allowlisted, %d excluded, %d started, %d past kickoff, %d malformed)",
        stats.total,
        stats.kept,
        stats.not_allowlisted,
        stats.excluded_competition,
        stats.already_started,
        stats.past_kickoff,
        stats.malformed,
    )
    return kept, stats
