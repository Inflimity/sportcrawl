"""
Form enrichment for SportCrawl's prediction engine.

The fixture dump carries no predictive data at all — just team names and
kickoff times — so every scoring rate has to be derived from each team's
recent results.

SofaScore's API sits behind Cloudflare and rejects plain HTTP clients (403),
so requests are issued from inside a Playwright page context, reusing the
stealth browser configuration the fixture monitor already relies on.

``team/{id}/events/last/0`` is the endpoint that matters: it returns a team's
recent matches with full scorelines. ``event/{id}/pregame-form`` is not
reliably present (404s on many fixtures) and is deliberately not used.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

from config.settings import Settings
from core.predictor.filter import Fixture
from core.predictor.leagues import is_form_eligible
from monitors.sofascore_monitor import SofaScoreMonitor

logger = logging.getLogger("SportCrawl.Predictor.Enrich")

API_BASE = "https://www.sofascore.com/api/v1"

# Below this many usable matches a team's rates are too noisy to bet on.
MIN_MATCHES_FOR_CONFIDENCE = 5


@dataclass
class TeamForm:
    """Scoring profile derived from a team's recent completed matches."""

    team_id: int
    name: str
    matches_used: int = 0

    gf_avg: float = 0.0          # goals scored per match
    ga_avg: float = 0.0          # goals conceded per match
    gf_home: Optional[float] = None
    ga_home: Optional[float] = None
    gf_away: Optional[float] = None
    ga_away: Optional[float] = None

    btts_rate: float = 0.0       # share of matches where both teams scored
    over25_rate: float = 0.0     # share of matches with 3+ total goals
    scored_rate: float = 0.0     # share of matches where this team scored
    clean_sheet_rate: float = 0.0

    recent_results: str = ""     # e.g. "WWDLW", most recent first

    @property
    def is_reliable(self) -> bool:
        return self.matches_used >= MIN_MATCHES_FOR_CONFIDENCE

    @property
    def non_loss_rate(self) -> float:
        """Share of recent matches won or drawn."""
        if not self.recent_results:
            return 0.0
        return sum(r in "WD" for r in self.recent_results) / len(self.recent_results)

    @property
    def non_win_rate(self) -> float:
        """Share of recent matches drawn or lost."""
        if not self.recent_results:
            return 0.0
        return sum(r in "DL" for r in self.recent_results) / len(self.recent_results)

    def attack_rate(self, at_home: bool) -> float:
        """Goals scored per match, venue-specific when the sample allows."""
        venue = self.gf_home if at_home else self.gf_away
        return venue if venue is not None else self.gf_avg

    def defence_rate(self, at_home: bool) -> float:
        """Goals conceded per match, venue-specific when the sample allows."""
        venue = self.ga_home if at_home else self.ga_away
        return venue if venue is not None else self.ga_avg


def _build_form(team_id: int, name: str, events: list[dict[str, Any]], limit: int) -> TeamForm:
    """Compute a TeamForm from raw SofaScore event objects."""
    form = TeamForm(team_id=team_id, name=name)

    usable = [
        ev
        for ev in events
        if (ev.get("status") or {}).get("type") == "finished"
        and is_form_eligible((ev.get("tournament") or {}).get("name", ""))
        and (ev.get("homeScore") or {}).get("current") is not None
        and (ev.get("awayScore") or {}).get("current") is not None
    ]
    usable.sort(key=lambda ev: ev.get("startTimestamp") or 0, reverse=True)
    usable = usable[:limit]

    if not usable:
        return form

    gf = ga = btts = over25 = scored = clean = 0
    home_gf = home_ga = home_n = 0
    away_gf = away_ga = away_n = 0
    results: list[str] = []

    for ev in usable:
        home_score = int(ev["homeScore"]["current"])
        away_score = int(ev["awayScore"]["current"])
        at_home = (ev.get("homeTeam") or {}).get("id") == team_id

        scored_by_team = home_score if at_home else away_score
        conceded_by_team = away_score if at_home else home_score

        gf += scored_by_team
        ga += conceded_by_team
        if home_score > 0 and away_score > 0:
            btts += 1
        if home_score + away_score > 2:
            over25 += 1
        if scored_by_team > 0:
            scored += 1
        if conceded_by_team == 0:
            clean += 1

        if at_home:
            home_gf += scored_by_team
            home_ga += conceded_by_team
            home_n += 1
        else:
            away_gf += scored_by_team
            away_ga += conceded_by_team
            away_n += 1

        if scored_by_team > conceded_by_team:
            results.append("W")
        elif scored_by_team < conceded_by_team:
            results.append("L")
        else:
            results.append("D")

    n = len(usable)
    form.matches_used = n
    form.gf_avg = gf / n
    form.ga_avg = ga / n
    form.btts_rate = btts / n
    form.over25_rate = over25 / n
    form.scored_rate = scored / n
    form.clean_sheet_rate = clean / n
    form.recent_results = "".join(results)

    # Venue splits are only meaningful with a couple of matches behind them.
    if home_n >= 2:
        form.gf_home = home_gf / home_n
        form.ga_home = home_ga / home_n
    if away_n >= 2:
        form.gf_away = away_gf / away_n
        form.ga_away = away_ga / away_n

    return form


async def fetch_team_forms(
    fixtures: list[Fixture],
    form_matches: int = 10,
    batch_size: int = 8,
    settings: Optional[Settings] = None,
) -> dict[int, TeamForm]:
    """
    Fetch and compute recent form for every team appearing in ``fixtures``.

    Returns a mapping of team id -> TeamForm. Teams whose history could not be
    retrieved are present with ``matches_used == 0`` so callers can skip them
    rather than silently treating them as goalless.
    """
    names: dict[int, str] = {}
    for fx in fixtures:
        if fx.home_id:
            names[fx.home_id] = fx.home_name
        if fx.away_id:
            names[fx.away_id] = fx.away_name

    if not names:
        return {}

    team_ids = list(names)
    logger.info("Fetching recent form for %d teams across %d fixtures...", len(team_ids), len(fixtures))

    monitor = SofaScoreMonitor(settings or Settings())
    browser, _context, page = await monitor._create_browser_context()

    raw_by_team: dict[int, list[dict[str, Any]]] = {}
    try:
        await page.goto("https://www.sofascore.com/football", wait_until="domcontentloaded", timeout=25000)
        await asyncio.sleep(1.5)
        total_batches = (len(team_ids) + batch_size - 1) // batch_size
        for batch_no, start in enumerate(range(0, len(team_ids), batch_size), 1):
            chunk = team_ids[start : start + batch_size]
            logger.info(
                "  form batch %d/%d (%d teams, %d fetched so far)",
                batch_no,
                total_batches,
                len(chunk),
                len(raw_by_team),
            )
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
                            out[id] = await fetchWithTimeout(`${base}/team/${id}/events/last/0`, 8000);
                        }));
                        return out;
                    }""",
                    {"ids": chunk, "base": API_BASE},
                )
                for key, events in (batch or {}).items():
                    if events:
                        raw_by_team[int(key)] = events
            except Exception as batch_err:
                logger.warning("Batch %d fetch error: %s", batch_no, batch_err)

            await asyncio.sleep(0.3)
    finally:
        try:
            await browser.close()
        except Exception:
            pass

    forms: dict[int, TeamForm] = {}
    for team_id, name in names.items():
        events = raw_by_team.get(team_id)
        if not events:
            logger.warning("No form history retrieved for %s (id=%s)", name, team_id)
            forms[team_id] = TeamForm(team_id=team_id, name=name)
            continue
        forms[team_id] = _build_form(team_id, name, events, form_matches)

    reliable = sum(1 for f in forms.values() if f.is_reliable)
    logger.info("Built form for %d teams (%d with a usable sample)", len(forms), reliable)
    return forms
