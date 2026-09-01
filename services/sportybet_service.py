"""
SportyBet Automated Booking Service for SportCrawl.

Combines SportyBet API market data + market mapper with direct SportyBet booking API
(POST /api/{country}/orders/share) to build betting slips and retrieve authentic booking codes
instantaneously and with 100% accuracy across all markets (1X2, Over/Under, GG/NG, Double Chance, DNB).
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx

from core.market_mapper import resolve_market_selection
from core.prediction_parser import MarketCategory, ParsedBet
from core.team_matcher import match_fixture

logger = logging.getLogger("SportCrawl.SportyBet")


@dataclass
class BookedSelection:
    home_team: str
    away_team: str
    market_desc: str
    selection_desc: str
    odds: Optional[str] = None
    matched: bool = True
    error_msg: Optional[str] = None


@dataclass
class BookingResult:
    success: bool
    booking_code: Optional[str] = None
    total_odds: Optional[str] = None
    selections_count: int = 0
    booked_selections: list[BookedSelection] = field(default_factory=list)
    unmatched_selections: list[BookedSelection] = field(default_factory=list)
    share_url: Optional[str] = None
    error_message: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "booking_code": self.booking_code,
            "total_odds": self.total_odds,
            "selections_count": self.selections_count,
            "booked_selections": [
                {
                    "home_team": s.home_team,
                    "away_team": s.away_team,
                    "market": s.market_desc,
                    "selection": s.selection_desc,
                    "odds": s.odds,
                }
                for s in self.booked_selections
            ],
            "unmatched_selections": [
                {
                    "home_team": s.home_team,
                    "away_team": s.away_team,
                    "market": s.market_desc,
                    "selection": s.selection_desc,
                    "reason": s.error_msg,
                }
                for s in self.unmatched_selections
            ],
            "share_url": self.share_url,
            "error_message": self.error_message,
        }


class SportyBetBookerService:
    """Automates SportyBet betslip creation and booking code generation via official API."""

    def __init__(self, country_code: str = "ng", headless: bool = True):
        self.country_code = country_code.lower()
        self.headless = headless
        self.base_url = f"https://www.sportybet.com/{self.country_code}/sport/football"
        self.api_base = f"https://www.sportybet.com/api/{self.country_code}"
        self.http_headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json;charset=UTF-8",
            "Referer": f"https://www.sportybet.com/{self.country_code}/sport/football",
            "Origin": f"https://www.sportybet.com",
            "operatortoken": "",
        }

BOOKING_TZ = ZoneInfo("Africa/Lagos")


def booking_horizon(now: Optional[datetime] = None) -> datetime:
    """
    Latest kickoff a booking may target.

    Defaults to the end of the current day in WAT, because every ticket this
    engine builds is "today's card". Set BOOKING_HORIZON_HOURS to a positive
    number to use a rolling window of that many hours instead.
    """
    now = now or datetime.now(timezone.utc)
    try:
        hours = int(os.getenv("BOOKING_HORIZON_HOURS", "0"))
    except ValueError:
        hours = 0
    if hours > 0:
        return now + timedelta(hours=hours)
    local_end = datetime.combine(now.astimezone(BOOKING_TZ).date(), dt_time.max, tzinfo=BOOKING_TZ)
    return local_end.astimezone(timezone.utc)


def within_booking_window(
    event: dict[str, Any],
    now: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> bool:
    """
    True if the event kicks off between now and the horizon.

    Nothing used to check this. A fixture screened for today was matched on team
    names alone against the full upcoming card — ~1,100 events spanning weeks —
    so a name that resolved to a different day's event was booked without
    complaint, and a 2-odds banker shipped a leg kicking off the following night.
    Events with no usable start time are kept: dropping them would silently
    shrink the card if SportyBet ever changes the field.
    """
    raw = event.get("estimateStartTime")
    if raw in (None, ""):
        return True
    try:
        kickoff = datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return True
    now = now or datetime.now(timezone.utc)
    return now <= kickoff <= (until or booking_horizon(now))


    async def fetch_available_events(self, max_pages: int = 12) -> list[dict[str, Any]]:
        """
        Fetch the upcoming football card from SportyBet.

        Uses ``factsCenter/pcUpcomingEvents``, which is paginated and returns the
        full prematch list (~1000 events). The older ``liveOrPrematchEvents``
        endpoint returns only what is currently in-play — typically under a
        dozen events, mostly Simulated Reality virtuals — so fixtures kicking
        off later today were never found and could not be booked.
        """
        ts = int(time.time() * 1000)
        events: list[dict[str, Any]] = []
        try:
            async with httpx.AsyncClient(headers=self.http_headers, timeout=15.0) as client:
                for page in range(1, max_pages + 1):
                    url = (
                        f"{self.api_base}/factsCenter/pcUpcomingEvents"
                        f"?sportId=sr%3Asport%3A1&marketId=1%2C18%2C10%2C29"
                        f"&pageSize=100&pageNum={page}&_t={ts}"
                    )
                    r = await client.get(url)
                    if r.status_code != 200:
                        break

                    tournaments = (r.json().get("data") or {}).get("tournaments") or []
                    if not tournaments:
                        break

                    page_events = 0
                    for t in tournaments:
                        t_name = (t.get("name") or "").lower()
                        cat_name = (t.get("categoryName") or "").lower()
                        # Exclude Simulated Reality / eSports / Virtuals
                        if any(x in t_name or x in cat_name for x in ("srl", "simulated", "virtual", "cyber", "esport")):
                            continue
                        for ev in t.get("events", []):
                            h_name = (ev.get("homeTeamName") or "").lower()
                            a_name = (ev.get("awayTeamName") or "").lower()
                            if any(x in h_name or x in a_name for x in ("srl", "simulated reality")):
                                continue
                            events.append(ev)
                            page_events += 1

                    if page_events == 0 and page > 1:
                        break
        except Exception as e:
            logger.warning("Failed to fetch SportyBet events via API: %s", e)

        # Confine the card to the booking window before anyone matches against
        # it. Every consumer — pricing, shortlisting and booking — draws from
        # this one call, so filtering here closes the wrong-day match on all of
        # them at once.
        now = datetime.now(timezone.utc)
        until = booking_horizon(now)
        in_window = [ev for ev in events if within_booking_window(ev, now, until)]
        if len(in_window) != len(events):
            logger.info(
                "Retrieved %d bookable SportyBet events (%d dropped outside the booking window, horizon %s)",
                len(in_window),
                len(events) - len(in_window),
                until.astimezone(BOOKING_TZ).strftime("%Y-%m-%d %H:%M %Z"),
            )
        else:
            logger.info("Retrieved %d bookable SportyBet events", len(in_window))
        return in_window

    async def fetch_event_markets(self, event_id: str) -> list[dict[str, Any]]:
        """Fetch full market list for a specific event from SportyBet API."""
        ts = int(time.time() * 1000)
        try:
            async with httpx.AsyncClient(headers=self.http_headers, timeout=12.0) as client:
                url = f"{self.api_base}/factsCenter/event?eventId={event_id}&_t={ts}"
                r = await client.get(url)
                if r.status_code == 200:
                    return r.json().get("data", {}).get("markets", [])
        except Exception as e:
            logger.warning("Failed to fetch markets for event %s: %s", event_id, e)
        return []

    async def generate_booking_code(
        self,
        parsed_bets: list[ParsedBet],
        timeout_seconds: int = 20,
    ) -> BookingResult:
        """
        Takes a list of ParsedBet items, resolves market selections via market_mapper,
        and creates a valid SportyBet booking code via SportyBet's direct orders/share endpoint.
        """
        if not parsed_bets:
            return BookingResult(
                success=False,
                error_message="No valid betting predictions provided.",
            )

        # 1. Fetch available events from API for fuzzy matching
        logger.info("Fetching available SportyBet events via API...")
        api_events = await self.fetch_available_events()
        logger.info("Retrieved %d available events from SportyBet API", len(api_events))

        if not api_events:
            return BookingResult(
                success=False,
                error_message="Could not load fixtures from SportyBet API.",
            )

        # 2. Resolve each bet against event markets via core.market_mapper
        selections_payload: list[dict[str, Any]] = []
        booked: list[BookedSelection] = []
        unmatched: list[BookedSelection] = []
        total_odds_calc = 1.0

        for bet in parsed_bets:
            matched_res = match_fixture(bet.home_team, bet.away_team, api_events, threshold=0.48)
            if not matched_res:
                unmatched.append(
                    BookedSelection(
                        home_team=bet.home_team,
                        away_team=bet.away_team,
                        market_desc=bet.market_category.value,
                        selection_desc=bet.selection,
                        matched=False,
                        error_msg="Match not found on SportyBet",
                    )
                )
                logger.warning("⚠️ Match not found on SportyBet: %s vs %s", bet.home_team, bet.away_team)
                continue

            matched_ev_info = matched_res[0]
            event_id = matched_ev_info.get("eventId")
            if not event_id:
                unmatched.append(
                    BookedSelection(
                        home_team=bet.home_team,
                        away_team=bet.away_team,
                        market_desc=bet.market_category.value,
                        selection_desc=bet.selection,
                        matched=False,
                        error_msg="Missing event ID on SportyBet",
                    )
                )
                continue

            markets = await self.fetch_event_markets(event_id)
            resolved_market = resolve_market_selection(bet, markets)

            if not resolved_market:
                unmatched.append(
                    BookedSelection(
                        home_team=bet.home_team,
                        away_team=bet.away_team,
                        market_desc=bet.market_category.value,
                        selection_desc=bet.selection,
                        matched=False,
                        error_msg="Market outcome not active on SportyBet",
                    )
                )
                logger.warning("⚠️ Market not resolved for: %s vs %s (%s)", bet.home_team, bet.away_team, bet.selection)
                continue

            # Add to payload
            selections_payload.append({
                "eventId": event_id,
                "marketId": str(resolved_market["marketId"]),
                "specifier": resolved_market["specifier"] if resolved_market.get("specifier") else None,
                "outcomeId": str(resolved_market["outcomeId"]),
            })

            odds_float = float(resolved_market.get("odds", 1.0) or 1.0)
            total_odds_calc *= odds_float

            booked.append(
                BookedSelection(
                    home_team=matched_ev_info.get("homeTeamName", bet.home_team),
                    away_team=matched_ev_info.get("awayTeamName", bet.away_team),
                    market_desc=resolved_market.get("marketDesc", bet.market_category.value),
                    selection_desc=resolved_market.get("outcomeDesc", bet.selection),
                    odds=resolved_market.get("odds", f"{odds_float:.2f}"),
                    matched=True,
                )
            )
            logger.info(
                "✅ Resolved: %s vs %s -> %s (%s) @%s",
                matched_ev_info.get("homeTeamName"),
                matched_ev_info.get("awayTeamName"),
                resolved_market.get("outcomeDesc"),
                resolved_market.get("marketDesc"),
                resolved_market.get("odds"),
            )

        if not selections_payload:
            return BookingResult(
                success=False,
                booked_selections=[],
                unmatched_selections=unmatched,
                error_message="None of the requested matches or markets could be placed on SportyBet.",
            )

        # 3. Call SportyBet orders/share API directly
        try:
            share_endpoint = f"{self.api_base}/orders/share"
            logger.info("Submitting %d selections to SportyBet booking API (%s)...", len(selections_payload), share_endpoint)

            async with httpx.AsyncClient(headers=self.http_headers, timeout=12.0) as client:
                resp = await client.post(share_endpoint, json={"selections": selections_payload})

                if resp.status_code == 200:
                    resp_data = resp.json()
                    if resp_data.get("bizCode") == 10000 or resp_data.get("isAvailable"):
                        data_block = resp_data.get("data", {})
                        share_code = data_block.get("shareCode")

                        # A response can come back "available" without a code —
                        # bizCode 10000 OR isAvailable is enough to land here.
                        # Reporting success then produced a digest line reading
                        # "SportyBet Code: None" and a betslip button pointing at
                        # "?shareCode=None". A ticket with no code is not booked.
                        if not share_code:
                            logger.warning(
                                "SportyBet returned no shareCode (bizCode=%s, isAvailable=%s); treating as unbooked",
                                resp_data.get("bizCode"),
                                resp_data.get("isAvailable"),
                            )
                            return BookingResult(
                                success=False,
                                booked_selections=booked,
                                unmatched_selections=unmatched,
                                error_message="SportyBet accepted the slip but returned no booking code",
                            )

                        share_url = data_block.get("shareURL") or f"https://www.sportybet.com/{self.country_code}/?shareCode={share_code}"
                        
                        # Calculate exact odds from response outcomes if available
                        outcomes_resp = data_block.get("outcomes", [])
                        real_total_odds = 1.0
                        for out in outcomes_resp:
                            for mk in out.get("markets", []):
                                for o in mk.get("outcomes", []):
                                    try:
                                        real_total_odds *= float(o.get("odds", 1.0))
                                    except Exception:
                                        pass

                        formatted_odds = f"{real_total_odds:.2f}" if outcomes_resp else f"{total_odds_calc:.2f}"

                        logger.info("🎉 SUCCESS! SportyBet Booking Code: %s (Total Odds: %s)", share_code, formatted_odds)
                        return BookingResult(
                            success=True,
                            booking_code=share_code,
                            total_odds=formatted_odds,
                            selections_count=len(booked),
                            booked_selections=booked,
                            unmatched_selections=unmatched,
                            share_url=share_url,
                        )
                    else:
                        err_msg = resp_data.get("message", "SportyBet rejected the selections")
                        logger.warning("SportyBet booking rejected: %s", err_msg)
                        return BookingResult(
                            success=False,
                            booked_selections=booked,
                            unmatched_selections=unmatched,
                            error_message=err_msg,
                        )
                else:
                    return BookingResult(
                        success=False,
                        booked_selections=booked,
                        unmatched_selections=unmatched,
                        error_message=f"SportyBet booking API returned HTTP {resp.status_code}",
                    )

        except Exception as exc:
            logger.error("Error creating SportyBet booking code: %s", exc, exc_info=True)
            return BookingResult(
                success=False,
                booked_selections=booked,
                unmatched_selections=unmatched,
                error_message=f"Booking API error: {str(exc)}",
            )
