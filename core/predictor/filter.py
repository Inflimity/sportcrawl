"""
Fixture filtering for SportCrawl's prediction engine.

Reduces a raw SofaScore dump (typically 150-250 fixtures spanning every
competition on earth) to the handful that are worth modelling.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.predictor.leagues import is_allowed, is_excluded

logger = logging.getLogger("SportCrawl.Predictor.Filter")


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
) -> tuple[list[Fixture], FilterStats]:
    """
    Keep only fixtures worth predicting on.

    Set ``allow_unlisted`` to bypass the competition allowlist while still
    applying the hard exclusions — useful for exploring coverage, not for
    generating real picks.
    """
    stats = FilterStats(total=len(raw_matches))
    kept: list[Fixture] = []

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
        "Filtered %d fixtures down to %d tradeable (%d not allowlisted, %d excluded, %d started, %d malformed)",
        stats.total,
        stats.kept,
        stats.not_allowlisted,
        stats.excluded_competition,
        stats.already_started,
        stats.malformed,
    )
    return kept, stats
