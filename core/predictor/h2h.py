"""
Head-to-head screening: pick the market these two teams keep producing.

This is a different question from the one ``screen.py`` asks. That model reads
each side's recent form independently and projects goals with Poisson. This
reads only what happens *when these two play each other*, and picks whichever
of Home / Away / Draw / GG / Over 2.5 that record supports best.

Three things this module refuses to get wrong:

**Orientation.** A past meeting can have the fixtures reversed — today's home
side was the away side then. Counting that meeting's "home win" as a home win
for today's home team is the obvious bug here, and it silently inverts the
result for roughly half the sample. Every meeting is re-oriented to today's
home team before anything is counted.

**Small samples.** Two teams typically meet a handful of times, and 4 out of 5
is not 80%. Raw rates are reported because that is the thing being observed,
but the probability used for selection is shrunk toward the market's base rate
with a strength of ``PRIOR_STRENGTH`` pseudo-meetings. On a 10-meeting record
the shrink barely moves the number; on a 3-meeting record it moves it a lot,
which is the point.

**Stale meetings.** A 2015 fixture tells you little about these squads. Only
meetings inside ``DEFAULT_MAX_AGE_DAYS`` are counted, and the cutoff is
reported so a thin record is visible rather than inferred.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from config.settings import Settings
from core.predictor.filter import Fixture

logger = logging.getLogger(__name__)

API_BASE = "https://www.sofascore.com/api/v1"

# Roughly six seasons. Long enough to have a record at all, short enough that
# it is about these clubs rather than their grandparents.
DEFAULT_MAX_AGE_DAYS = 2200

# Fewest meetings that can support a selection. Below this the shrink would
# dominate the observation anyway, so the fixture is skipped instead of
# dressing up a prior as a finding.
DEFAULT_MIN_MEETINGS = 4

# Weight of the prior, in pseudo-meetings. At 4, a 4-meeting record is half
# observation and half base rate.
PRIOR_STRENGTH = 4.0

# League-average base rates, used only as the shrink target.
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


@dataclass
class Meeting:
    """One past match between the two teams, oriented to today's home side."""

    timestamp: int
    goals_for_home: int   # goals scored by the team that is home TODAY
    goals_for_away: int   # goals scored by the team that is away TODAY

    @property
    def total_goals(self) -> int:
        return self.goals_for_home + self.goals_for_away

    @property
    def home_won(self) -> bool:
        return self.goals_for_home > self.goals_for_away

    @property
    def away_won(self) -> bool:
        return self.goals_for_away > self.goals_for_home

    @property
    def drawn(self) -> bool:
        return self.goals_for_home == self.goals_for_away

    @property
    def btts(self) -> bool:
        return self.goals_for_home > 0 and self.goals_for_away > 0

    @property
    def over_25(self) -> bool:
        return self.total_goals > 2


@dataclass
class H2HRecord:
    """What these two have produced against each other, and how much of it."""

    fixture: Fixture
    meetings: list[Meeting] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.meetings)

    def hits(self, selection: str) -> int:
        tests = {
            "1": lambda m: m.home_won,
            "2": lambda m: m.away_won,
            "X": lambda m: m.drawn,
            "GG": lambda m: m.btts,
            "Over 2.5": lambda m: m.over_25,
        }
        test = tests[selection]
        return sum(1 for m in self.meetings if test(m))

    def raw_rate(self, selection: str) -> float:
        """What actually happened, undiluted. Reported, not selected on."""
        return self.hits(selection) / self.count if self.count else 0.0

    def shrunk_probability(self, selection: str, prior_strength: float = PRIOR_STRENGTH) -> float:
        """The rate pulled toward the base rate by sample size."""
        if not self.count:
            return BASE_RATES[selection]
        prior = BASE_RATES[selection]
        return (self.hits(selection) + prior_strength * prior) / (self.count + prior_strength)

    def summary(self) -> str:
        parts = [f"{self.count} meetings"]
        for sel in ("1", "X", "2", "GG", "Over 2.5"):
            parts.append(f"{sel} {self.hits(sel)}/{self.count}")
        return ", ".join(parts)


def best_selection(
    record: H2HRecord,
    min_meetings: int = DEFAULT_MIN_MEETINGS,
    markets: Optional[list[str]] = None,
) -> Optional[tuple[str, float, float]]:
    """
    The market this record supports best.

    Returns ``(selection, shrunk_probability, raw_rate)``, or ``None`` when the
    record is too thin to say anything. Selection is on the shrunk figure so a
    3-from-3 record cannot outrank a 9-from-12 one on noise alone.
    """
    if record.count < min_meetings:
        return None

    # Order is the tie-break, and ties are common: a 3-1 record makes the home
    # win, GG and Over 2.5 all perfect. max() keeps the first maximum, so this
    # list is a preference order, not an arbitrary one — the goal markets sit
    # ahead of the result markets because they need only the pattern to repeat,
    # not the same side to win again.
    candidates = markets or ["GG", "Over 2.5", "1", "2", "X"]
    best = max(candidates, key=lambda s: record.shrunk_probability(s))
    return best, record.shrunk_probability(best), record.raw_rate(best)


def _orient(event: dict[str, Any], home_id: int) -> Optional[Meeting]:
    """Turn a raw SofaScore event into a Meeting seen from today's home team."""
    try:
        h_id = int(event["homeTeam"]["id"])
        a_id = int(event["awayTeam"]["id"])
        h_goals = event.get("homeScore", {}).get("current")
        a_goals = event.get("awayScore", {}).get("current")
        ts = int(event.get("startTimestamp") or 0)
    except (KeyError, TypeError, ValueError):
        return None

    if h_goals is None or a_goals is None:
        return None  # not played, or abandoned

    if h_id == home_id:
        return Meeting(timestamp=ts, goals_for_home=int(h_goals), goals_for_away=int(a_goals))
    if a_id == home_id:
        # Reversed fixture: today's home side was away in this meeting.
        return Meeting(timestamp=ts, goals_for_home=int(a_goals), goals_for_away=int(h_goals))
    return None  # neither team matches; not a meeting between these two


def build_record(
    fixture: Fixture,
    raw_events: list[dict[str, Any]],
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    before_ts: Optional[int] = None,
) -> H2HRecord:
    """
    Assemble a record from raw SofaScore events.

    ``before_ts`` excludes meetings at or after that timestamp. Pass the
    fixture's own kickoff when backtesting, or the record leaks results the
    prediction could not have known — the same trap ``fetch_team_forms``
    guards with its own ``before_ts``.
    """
    cutoff = int(time.time()) - max_age_days * 86400
    meetings: list[Meeting] = []
    for event in raw_events or []:
        meeting = _orient(event, fixture.home_id)
        if not meeting:
            continue
        if meeting.timestamp < cutoff:
            continue
        if before_ts is not None and meeting.timestamp >= before_ts:
            continue
        meetings.append(meeting)

    meetings.sort(key=lambda m: m.timestamp, reverse=True)
    return H2HRecord(fixture=fixture, meetings=meetings)


async def fetch_h2h(
    fixtures: list[Fixture],
    batch_size: int = 8,
    settings: Optional[Settings] = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    before_ts: Optional[int] = None,
) -> dict[int, H2HRecord]:
    """
    Fetch previous meetings for every fixture, keyed by ``fixture.match_id``.

    Mirrors ``fetch_team_forms``: one browser context, requests issued from
    inside the page so Cloudflare sees a real session, batched and paced.
    SofaScore throttles hard under load and a blocked IP costs the whole run,
    so callers should pass only the fixtures they actually intend to pick from.
    """
    if not fixtures:
        return {}

    from monitors.sofascore_monitor import SofaScoreMonitor

    ids = [fx.match_id for fx in fixtures if fx.match_id]
    by_id = {fx.match_id: fx for fx in fixtures if fx.match_id}
    logger.info("Fetching head-to-head for %d fixtures...", len(ids))

    monitor = SofaScoreMonitor(settings or Settings())
    browser, _context, page = await monitor._create_browser_context()

    raw: dict[int, list[dict[str, Any]]] = {}
    try:
        await page.goto(
            "https://www.sofascore.com/football", wait_until="domcontentloaded", timeout=25000
        )
        await asyncio.sleep(1.5)

        total_batches = (len(ids) + batch_size - 1) // batch_size
        for batch_no, start in enumerate(range(0, len(ids), batch_size), 1):
            chunk = ids[start : start + batch_size]
            logger.info("  h2h batch %d/%d (%d fixtures)", batch_no, total_batches, len(chunk))
            try:
                batch = await page.evaluate(
                    """async ({ids, base}) => {
                        const out = {};
                        const fetchWithTimeout = (url, ms = 8000) => {
                            return new Promise((resolve) => {
                                const timer = setTimeout(() => resolve(null), ms);
                                fetch(url)
                                    .then(r => r.ok ? r.json() : null)
                                    .then(data => {
                                        clearTimeout(timer);
                                        resolve(data ? (data.events || []) : null);
                                    })
                                    .catch(() => {
                                        clearTimeout(timer);
                                        resolve(null);
                                    });
                            });
                        };
                        await Promise.all(ids.map(async (id) => {
                            out[id] = await fetchWithTimeout(`${base}/event/${id}/h2h/events`, 8000);
                        }));
                        return out;
                    }""",
                    {"ids": chunk, "base": API_BASE},
                )
                for key, events in (batch or {}).items():
                    if events:
                        raw[int(key)] = events
            except Exception as batch_err:
                logger.warning("H2H batch %d failed: %s", batch_no, batch_err)

            await asyncio.sleep(0.3)
    finally:
        try:
            await browser.close()
        except Exception:
            pass

    records: dict[int, H2HRecord] = {}
    for match_id, fixture in by_id.items():
        records[match_id] = build_record(
            fixture, raw.get(match_id, []), max_age_days=max_age_days, before_ts=before_ts
        )

    thin = sum(1 for r in records.values() if r.count < DEFAULT_MIN_MEETINGS)
    logger.info(
        "H2H: %d fixtures, %d with fewer than %d meetings.",
        len(records), thin, DEFAULT_MIN_MEETINGS,
    )
    return records


def screen_h2h(
    records: dict[int, H2HRecord],
    min_meetings: int = DEFAULT_MIN_MEETINGS,
    limit: Optional[int] = None,
    markets: Optional[list[str]] = None,
) -> list["Pick"]:
    """
    Turn H2H records into ranked picks, best-supported first.

    Emits at most one pick per fixture — the market that record supports best —
    so the output drops straight into the existing pricing, capping and booking
    path without any of it needing to know where the picks came from.

    Fixtures with too few meetings produce nothing. That is the intended
    behaviour: this method reads a record, and a fixture without one has
    nothing to read.
    """
    from core.predictor.screen import Pick

    picks: list[tuple[float, "Pick"]] = []
    for record in records.values():
        chosen = best_selection(record, min_meetings=min_meetings, markets=markets)
        if not chosen:
            continue
        selection, probability, raw = chosen
        picks.append((
            probability,
            Pick(
                fixture=record.fixture,
                market=MARKET_LABELS[selection],
                selection=selection,
                probability=probability,
                # Conviction is the raw rate: how strongly the record itself
                # points this way, before the sample-size discount. Keeping both
                # means a 12-meeting 9/12 is distinguishable from a 4/4.
                conviction=raw,
                rationale=(
                    f"H2H {record.hits(selection)}/{record.count} "
                    f"({raw:.0%} raw, {probability:.0%} after sample-size shrink)"
                ),
            ),
        ))

    picks.sort(key=lambda item: item[0], reverse=True)
    ranked = [pick for _, pick in picks]
    return ranked[:limit] if limit else ranked
