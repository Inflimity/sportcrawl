"""
Post-match statistics from SofaScore: corners, cards, shots.

WHY THIS EXISTS
---------------
ANALYSIS.md §8 records that we found no repeatable edge on corners. The reason
given there matters more than the result: the model was built on *public season
averages*, which are noisy, measured against average opposition, and — per §6 —
frequently return corners TAKEN when you ask for corners CONCEDED.

This module removes that excuse. It banks per-match corner counts from the same
source we already grade goals against, so that a corner model can eventually be
built on real match-level history with a proper opponent adjustment, rather than
on a season average scraped from a site that may be lying about which column it
is handing you.

It does NOT make corners bettable. Until a sample accumulates, every corner
number in this project remains unvalidated, and §8 still stands: if your analysis
concludes a corner bet is +EV, assume you have made one of the six mistakes.

FETCHING
--------
Statistics live on a different endpoint from the event itself
(`/event/{id}/statistics`) and only exist once a match is finished. We reuse
enrich.py's in-page fetch: SofaScore blocks bare HTTP clients, so requests are
issued from inside a real browser page that has already loaded the site.

Coverage is partial by design. Smaller leagues frequently publish no statistics
at all, and a missing row means "not published", never "zero corners". Callers
must treat None as unknown — an absent corner count that reads as 0 would drag
every average toward zero and manufacture edge on unders.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

from config.settings import Settings

logger = logging.getLogger("SportCrawl.Predictor.MatchStats")

API_BASE = "https://www.sofascore.com/api/v1"

# SofaScore's statistics payload is grouped by period ("ALL", "1ST", "2ND") and
# within each period by category. These are the item names we care about, mapped
# to our field names. The names are stable but the grouping is not, so we search
# rather than index.
_WANTED = {
    "Corner kicks": "corners",
    "Corners": "corners",
    "Shots on target": "shots_on_target",
    "Yellow cards": "yellow_cards",
    "Ball possession": "possession",
}


@dataclass
class MatchStatLine:
    """One finished match's statistics. Every field may be None."""

    match_id: int
    home_corners: Optional[int] = None
    away_corners: Optional[int] = None
    home_corners_ht: Optional[int] = None
    away_corners_ht: Optional[int] = None
    home_shots_on_target: Optional[int] = None
    away_shots_on_target: Optional[int] = None
    home_yellow_cards: Optional[int] = None
    away_yellow_cards: Optional[int] = None
    home_possession: Optional[int] = None

    @property
    def has_corners(self) -> bool:
        return self.home_corners is not None and self.away_corners is not None

    @property
    def total_corners(self) -> Optional[int]:
        if not self.has_corners:
            return None
        return self.home_corners + self.away_corners


def _as_int(value: Any) -> Optional[int]:
    """
    SofaScore reports possession as "62%" and most counts as plain ints, but a
    few come through as strings, and shot stats sometimes arrive as "6/11".
    Anything we cannot read cleanly becomes None rather than a guess.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip().rstrip("%")
    if "/" in text:                      # "6/11" -> the first number
        text = text.split("/", 1)[0].strip()
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def parse_statistics(match_id: int, payload: dict[str, Any] | None) -> MatchStatLine:
    """
    Turn one `/event/{id}/statistics` response into a MatchStatLine.

    Pure and offline, so it is testable without a browser. An unparseable or
    empty payload yields a line with every field None — which is the correct
    representation of "this match published no statistics".
    """
    line = MatchStatLine(match_id=match_id)
    if not payload:
        return line

    for period_block in payload.get("statistics", []) or []:
        period = period_block.get("period")
        if period not in ("ALL", "1ST"):
            continue
        for group in period_block.get("groups", []) or []:
            for item in group.get("statisticsItems", []) or []:
                field = _WANTED.get(item.get("name", ""))
                if not field:
                    continue
                home = _as_int(item.get("homeValue", item.get("home")))
                away = _as_int(item.get("awayValue", item.get("away")))

                if field == "corners":
                    if period == "ALL":
                        line.home_corners, line.away_corners = home, away
                    else:
                        line.home_corners_ht, line.away_corners_ht = home, away
                elif period != "ALL":
                    # Only corners are worth a half-time split; everything else
                    # is recorded full-match only.
                    continue
                elif field == "shots_on_target":
                    line.home_shots_on_target, line.away_shots_on_target = home, away
                elif field == "yellow_cards":
                    line.home_yellow_cards, line.away_yellow_cards = home, away
                elif field == "possession":
                    line.home_possession = home

    return line


async def fetch_match_stats(
    match_ids: list[int],
    batch_size: int = 6,
    settings: Optional[Settings] = None,
) -> dict[int, MatchStatLine]:
    """
    Fetch statistics for finished matches.

    Returns a mapping of match_id -> MatchStatLine for every id that answered.
    Ids that returned nothing are simply absent: the caller decides whether that
    is worth retrying, and a match with no published statistics never becomes a
    row of zeroes.

    Batched and paced like fetch_team_forms, for the same reason — SofaScore
    starts dropping requests under sustained load, and a silent drop here would
    look identical to a league that publishes no statistics.
    """
    if not match_ids:
        return {}

    from monitors.sofascore_monitor import SofaScoreMonitor

    monitor = SofaScoreMonitor(settings or Settings())
    browser, _context, page = await monitor._create_browser_context()

    out: dict[int, MatchStatLine] = {}
    try:
        await page.goto(
            "https://www.sofascore.com/football", wait_until="domcontentloaded", timeout=25000
        )
        await asyncio.sleep(1.5)

        total_batches = (len(match_ids) + batch_size - 1) // batch_size
        for batch_no, start in enumerate(range(0, len(match_ids), batch_size), 1):
            chunk = match_ids[start : start + batch_size]
            logger.info(
                "  stats batch %d/%d (%d matches, %d fetched so far)",
                batch_no,
                total_batches,
                len(chunk),
                len(out),
            )
            try:
                batch = await page.evaluate(
                    """async ({ids, base}) => {
                        const out = {};
                        const fetchWithTimeout = (url, ms = 8000) => new Promise((resolve) => {
                            const timer = setTimeout(() => resolve(null), ms);
                            fetch(url)
                                .then(r => r.ok ? r.json() : null)
                                .then(data => { clearTimeout(timer); resolve(data); })
                                .catch(() => { clearTimeout(timer); resolve(null); });
                        });
                        await Promise.all(ids.map(async (id) => {
                            out[id] = await fetchWithTimeout(`${base}/event/${id}/statistics`, 8000);
                        }));
                        return out;
                    }""",
                    {"ids": chunk, "base": API_BASE},
                )
                for key, payload in (batch or {}).items():
                    line = parse_statistics(int(key), payload)
                    # Store only if something was actually published. A line of
                    # all-None is indistinguishable from a failed request, and
                    # persisting it would stop us retrying later.
                    if line.has_corners or line.home_shots_on_target is not None:
                        out[int(key)] = line
            except Exception as batch_err:
                logger.warning("Stats batch %d fetch error: %s", batch_no, batch_err)

            await asyncio.sleep(0.3)

        missing = [m for m in match_ids if m not in out]
        if missing:
            logger.info(
                "%d of %d matches returned no statistics (small leagues often publish none)",
                len(missing),
                len(match_ids),
            )
    finally:
        try:
            await browser.close()
        except Exception:
            pass

    logger.info("Fetched statistics for %d/%d matches", len(out), len(match_ids))
    return out
