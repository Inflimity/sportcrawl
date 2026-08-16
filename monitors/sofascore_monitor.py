"""
SofaScore Monitor — Automated Football Match & Fixture Scraper.

Uses Playwright with stealth browser context to navigate SofaScore,
intercept and evaluate match data, normalize fixtures, and track live score changes.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Optional

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from config.settings import Settings

logger = logging.getLogger(__name__)

# Key SofaScore Unique Tournament IDs for thorough fixture scraping
POPULAR_TOURNAMENT_IDS = [
    17,     # Premier League
    7,      # UEFA Champions League
    679,    # UEFA Europa League
    17015,  # UEFA Conference League
    8,      # LaLiga
    23,     # Serie A
    35,     # Bundesliga
    34,     # Ligue 1
    19,     # FA Cup
    325,    # Brasileirão Betano
    242,    # Major League Soccer
    346,    # Community Shield
    339,    # Trophee des Champions
    37,     # Eredivisie
    38,     # Liga Portugal Betclic
    52,     # Pro League (Belgium)
    238,    # Premiership (Scotland)
    2058,   # Super Lig (Turkey)
    1,      # World Cup
    16,     # European Championship
    134,    # Copa América
    270,    # Africa Cup of Nations
]


class SofaScoreMonitor:
    """Monitors SofaScore for today's football fixtures and live updates."""

    def __init__(self, settings: Settings) -> None:
        self.name = "SofaScoreMonitor"
        self.settings = settings
        self._running = False
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._callbacks: list[Callable[[dict[str, Any]], Coroutine[Any, Any, None]]] = []
        self._cycle_complete_callbacks: list[Callable[[list[dict[str, Any]]], Coroutine[Any, Any, None]]] = []
        self._featured_leagues_set = {
            league.lower() for league in (self.settings.featured_leagues if isinstance(self.settings.featured_leagues, list) else [self.settings.featured_leagues])
        }

    def on_match(self, callback: Callable[[dict[str, Any]], Coroutine[Any, Any, None]]) -> None:
        """Register a callback for newly scraped or updated match data."""
        self._callbacks.append(callback)

    def on_cycle_complete(self, callback: Callable[[list[dict[str, Any]]], Coroutine[Any, Any, None]]) -> None:
        """Register a callback invoked when a full scrape cycle completes."""
        self._cycle_complete_callbacks.append(callback)

    async def _create_browser_context(self) -> tuple[Browser, BrowserContext, Page]:
        """Launch Playwright Chromium with stealth headers."""
        if not self._playwright:
            self._playwright = await async_playwright().start()
        
        browser = await self._playwright.chromium.launch(
            headless=self.settings.sofascore_headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        return browser, context, page

    def _normalize_event(self, event: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Normalize a raw SofaScore event JSON into a clean dictionary."""
        try:
            match_id = event.get("id")
            if not match_id:
                return None

            tournament = event.get("tournament", {})
            tournament_name = tournament.get("name", "Unknown Tournament")
            category_name = tournament.get("category", {}).get("name", "International")
            
            # Check if featured league
            is_featured = (
                tournament_name.lower() in self._featured_leagues_set
                or any(fl in tournament_name.lower() for fl in self._featured_leagues_set)
            )

            home_team_data = event.get("homeTeam", {})
            away_team_data = event.get("awayTeam", {})
            home_team = home_team_data.get("name", "Home Team")
            away_team = away_team_data.get("name", "Away Team")

            status = event.get("status", {})
            status_type = status.get("type", "notstarted")
            status_description = status.get("description", "Not started")

            home_score_data = event.get("homeScore", {})
            away_score_data = event.get("awayScore", {})
            
            home_score = home_score_data.get("current")
            away_score = away_score_data.get("current")
            home_score_ht = home_score_data.get("period1")
            away_score_ht = away_score_data.get("period1")

            start_timestamp = event.get("startTimestamp", 0)
            if start_timestamp:
                start_time = datetime.fromtimestamp(start_timestamp, tz=timezone.utc)
                match_date = start_time.strftime("%Y-%m-%d")
            else:
                start_time = datetime.now(timezone.utc)
                match_date = start_time.strftime("%Y-%m-%d")

            slug = event.get("slug", f"{home_team.lower()}-{away_team.lower()}").replace(" ", "-")
            custom_id = event.get("customId", "")
            sofascore_url = f"https://www.sofascore.com/{slug}/{custom_id or match_id}"

            round_info = ""
            r_info = event.get("roundInfo", {})
            if isinstance(r_info, dict):
                round_info = r_info.get("name", "") or (f"Round {r_info.get('round')}" if r_info.get("round") else "")

            return {
                "match_id": match_id,
                "slug": slug,
                "tournament_name": tournament_name,
                "category_name": category_name,
                "round_info": round_info,
                "is_featured": is_featured,
                "home_team": home_team,
                "home_team_id": home_team_data.get("id"),
                "away_team": away_team,
                "away_team_id": away_team_data.get("id"),
                "start_timestamp": start_timestamp,
                "start_time": start_time,
                "match_date": match_date,
                "status_type": status_type,
                "status_description": status_description,
                "home_score": home_score,
                "away_score": away_score,
                "home_score_ht": home_score_ht,
                "away_score_ht": away_score_ht,
                "minute": event.get("time", {}).get("played"),
                "sofascore_url": sofascore_url,
                "raw": event,
            }
        except Exception as e:
            logger.warning("Error normalizing SofaScore event: %s", e)
            return None

    async def fetch_today_matches(self, date_str: Optional[str] = None) -> list[dict[str, Any]]:
        """
        Fetch and parse all football matches scheduled for today from SofaScore.
        Returns a deduplicated list of normalized match dicts.
        """
        if not date_str:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        logger.info("Opening SofaScore to fetch football fixtures for %s...", date_str)
        browser, context, page = await self._create_browser_context()

        collected_events: dict[int, dict[str, Any]] = {}

        # 1. Listen for scheduled-events responses when loading the page
        async def on_response(resp):
            if "scheduled-events" in resp.url and resp.status == 200:
                try:
                    data = await resp.json()
                    for ev in data.get("events", []):
                        if "id" in ev:
                            collected_events[ev["id"]] = ev
                except Exception:
                    pass

        page.on("response", on_response)

        try:
            # Navigate to football section
            await page.goto(
                "https://www.sofascore.com/football",
                wait_until="domcontentloaded",
                timeout=self.settings.sofascore_timeout_ms,
            )
            await page.wait_for_timeout(3000)

            # 2. Fetch all global categories and popular tournaments concurrently
            logger.info("Querying global categories & tournaments in session context...")
            global_events = await page.evaluate(
                """async ({tournaments, targetDate}) => {
                    const results = [];
                    
                    // 1. Query popular tournaments
                    for (const tid of tournaments) {
                        try {
                            const res = await fetch(`https://www.sofascore.com/api/v1/unique-tournament/${tid}/scheduled-events/${targetDate}`);
                            if (res.ok) {
                                const json = await res.json();
                                if (json.events) results.push(...json.events);
                            }
                        } catch (err) {}
                    }

                    // 2. Query all football categories globally
                    try {
                        const catRes = await fetch('https://www.sofascore.com/api/v1/sport/football/categories/all');
                        if (catRes.ok) {
                            const catData = await catRes.json();
                            const categories = catData.categories || [];
                            
                            // Fetch categories in parallel batches of 15
                            const batchSize = 15;
                            for (let i = 0; i < categories.length; i += batchSize) {
                                const batch = categories.slice(i, i + batchSize);
                                const batchPromises = batch.map(async (cat) => {
                                    try {
                                        const r = await fetch(`https://www.sofascore.com/api/v1/category/${cat.id}/scheduled-events/${targetDate}`);
                                        if (r.ok) {
                                            const d = await r.json();
                                            return d.events || [];
                                        }
                                    } catch (e) {}
                                    return [];
                                });
                                const batchResults = await Promise.all(batchPromises);
                                for (const evs of batchResults) {
                                    results.push(...evs);
                                }
                            }
                        }
                    } catch (catErr) {}

                    return results;
                }""",
                {"tournaments": POPULAR_TOURNAMENT_IDS, "targetDate": date_str},
            )

            for ev in global_events:
                if "id" in ev:
                    collected_events[ev["id"]] = ev

            logger.info("Captured %d raw match events worldwide from SofaScore", len(collected_events))

        except Exception as e:
            logger.error("Error fetching matches from SofaScore: %s", e)
        finally:
            await browser.close()

        # Normalize and filter
        normalized_matches = []
        for ev in collected_events.values():
            norm = self._normalize_event(ev)
            if norm:
                normalized_matches.append(norm)

        # Sort: featured first, then kickoff timestamp, then league
        normalized_matches.sort(
            key=lambda m: (
                not m["is_featured"],
                m["start_timestamp"],
                m["tournament_name"],
            )
        )

        logger.info(
            "Successfully parsed %d football fixtures for %s (%d featured)",
            len(normalized_matches),
            date_str,
            sum(1 for m in normalized_matches if m["is_featured"]),
        )
        return normalized_matches

    async def start(self) -> None:
        """Start periodic background polling of SofaScore matches and live scores."""
        self._running = True
        logger.info(
            "SofaScoreMonitor started — polling every %d seconds",
            self.settings.sofascore_poll_interval_seconds,
        )

        while self._running:
            try:
                matches = await self.fetch_today_matches()
                for match in matches:
                    for callback in self._callbacks:
                        try:
                            await callback(match)
                        except Exception as cb_err:
                            logger.error("Error in SofaScore callback: %s", cb_err)

                # Trigger cycle complete callbacks (e.g. sending matches document)
                for cycle_cb in self._cycle_complete_callbacks:
                    try:
                        await cycle_cb(matches)
                    except Exception as cycle_err:
                        logger.error("Error in cycle complete callback: %s", cycle_err)

            except Exception as poll_err:
                logger.error("Error in SofaScore polling cycle: %s", poll_err)

            # Wait for next poll interval
            for _ in range(self.settings.sofascore_poll_interval_seconds):
                if not self._running:
                    break
                await asyncio.sleep(1)

    async def stop(self) -> None:
        """Stop monitor and clean up browser process."""
        self._running = False
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("SofaScoreMonitor stopped cleanly")
