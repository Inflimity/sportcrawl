"""
REST API routes for the SofaScore Football Bot dashboard.

Provides endpoints for today's football fixtures, live scores,
tournament filtering, bookmarks, and on-demand scraping.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from storage.models import FootballMatch

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["football"])

_db = None
_engine = None
_settings = None
_monitor = None


def init_routes(db, engine, settings, monitor=None) -> None:
    """Inject dependencies into the routes module."""
    global _db, _engine, _settings, _monitor
    _db = db
    _engine = engine
    _settings = settings
    _monitor = monitor


# ── Response Models ──────────────────────────────────────────────────


class MatchResponse(BaseModel):
    id: int
    match_id: int
    tournament_name: str
    category_name: str
    round_info: str
    is_featured: bool
    home_team: str
    away_team: str
    start_timestamp: int
    start_time: str
    match_date: str
    status_type: str
    status_description: str
    home_score: Optional[int]
    away_score: Optional[int]
    home_score_ht: Optional[int]
    away_score_ht: Optional[int]
    minute: Optional[str]
    sofascore_url: str
    bookmarked: bool


class StatusResponse(BaseModel):
    status: str
    uptime_seconds: float
    stats: dict
    active_monitor: str
    featured_leagues_count: int


class ActionResponse(BaseModel):
    success: bool
    message: str


_start_time = datetime.now(timezone.utc)


def _serialize_match(m: FootballMatch) -> MatchResponse:
    """Convert FootballMatch ORM model to MatchResponse."""
    return MatchResponse(
        id=m.id,
        match_id=m.match_id,
        tournament_name=m.tournament_name,
        category_name=m.category_name,
        round_info=m.round_info,
        is_featured=m.is_featured,
        home_team=m.home_team,
        away_team=m.away_team,
        start_timestamp=m.start_timestamp,
        start_time=m.start_time.isoformat() if m.start_time else "",
        match_date=m.match_date,
        status_type=m.status_type,
        status_description=m.status_description,
        home_score=m.home_score,
        away_score=m.away_score,
        home_score_ht=m.home_score_ht,
        away_score_ht=m.away_score_ht,
        minute=m.minute,
        sofascore_url=m.sofascore_url,
        bookmarked=m.bookmarked,
    )


# ── Endpoints ────────────────────────────────────────────────────────


@router.get("/matches/today", response_model=list[MatchResponse])
async def get_today_matches(
    featured_only: bool = Query(False),
    status: Optional[str] = Query(None),
    league: Optional[str] = Query(None),
):
    """Fetch today's scheduled football matches."""
    if _db is None:
        raise HTTPException(503, "Database not initialized")

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    matches = await _db.get_matches_for_date(
        today_str, featured_only=featured_only, status=status, league=league
    )
    return [_serialize_match(m) for m in matches]


@router.get("/matches/live", response_model=list[MatchResponse])
async def get_live_matches():
    """Fetch all currently live football matches."""
    if _db is None:
        raise HTTPException(503, "Database not initialized")

    matches = await _db.get_live_matches()
    return [_serialize_match(m) for m in matches]


@router.get("/matches/tournaments", response_model=list[str])
async def get_tournaments():
    """Fetch list of distinct tournaments playing today."""
    if _db is None:
        raise HTTPException(503, "Database not initialized")

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return await _db.get_all_tournaments_today(today_str)


@router.post("/matches/{match_id}/bookmark", response_model=ActionResponse)
async def toggle_bookmark(match_id: int):
    """Toggle bookmark / watch status for a match."""
    if _db is None:
        raise HTTPException(503, "Database not initialized")

    new_state = await _db.toggle_bookmark(match_id)
    state_str = "bookmarked" if new_state else "unbookmarked"
    return ActionResponse(success=True, message=f"Match {match_id} {state_str}")


@router.post("/scrape/trigger", response_model=ActionResponse)
async def trigger_scrape():
    """Trigger an immediate scrape from SofaScore."""
    if _monitor is None or _db is None:
        raise HTTPException(503, "Monitor not initialized")

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    matches = await _monitor.fetch_today_matches(today_str)
    for m in matches:
        await _db.upsert_match(m, is_featured=m.get("is_featured", False))

    return ActionResponse(
        success=True,
        message=f"Successfully scraped {len(matches)} fixtures from SofaScore for {today_str}",
    )


@router.get("/export/txt")
async def export_txt(filter: str = Query("all")):
    """Export matches as downloadable plain-text document."""
    if _db is None:
        raise HTTPException(503, "Database not initialized")
    from notifiers.telegram_bot import LAGOS_TZ, generate_matches_txt
    from fastapi.responses import Response

    today_str = datetime.now(LAGOS_TZ).strftime("%Y-%m-%d")
    all_matches = await _db.get_matches_for_date(today_str)
    
    if filter == "upcoming":
        matches = [m for m in all_matches if m.status_type == "notstarted"]
        title = "Upcoming Football Fixtures"
        filename = f"sportcrawl_upcoming_{today_str}.txt"
    elif filter == "live":
        matches = [m for m in all_matches if m.status_type == "inprogress"]
        title = "Live In-Play Football Matches"
        filename = f"sportcrawl_live_{today_str}.txt"
    elif filter == "top":
        matches = [m for m in all_matches if m.is_featured]
        title = "Top Leagues & Featured Matches"
        filename = f"sportcrawl_top_{today_str}.txt"
    else:
        matches = all_matches
        title = "Today's Football Fixtures & Results"
        filename = f"sportcrawl_matches_{today_str}.txt"

    bio = generate_matches_txt(matches, today_str, title_override=title)
    return Response(
        content=bio.getvalue(),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@router.get("/export/json")
async def export_json(filter: str = Query("all")):
    """Export matches as downloadable JSON document."""
    if _db is None:
        raise HTTPException(503, "Database not initialized")
    from notifiers.telegram_bot import LAGOS_TZ, generate_matches_json
    from fastapi.responses import Response

    today_str = datetime.now(LAGOS_TZ).strftime("%Y-%m-%d")
    all_matches = await _db.get_matches_for_date(today_str)
    
    if filter == "upcoming":
        matches = [m for m in all_matches if m.status_type == "notstarted"]
        filename = f"sportcrawl_upcoming_{today_str}.json"
    elif filter == "live":
        matches = [m for m in all_matches if m.status_type == "inprogress"]
        filename = f"sportcrawl_live_{today_str}.json"
    elif filter == "top":
        matches = [m for m in all_matches if m.is_featured]
        filename = f"sportcrawl_top_{today_str}.json"
    else:
        matches = all_matches
        filename = f"sportcrawl_matches_{today_str}.json"

    bio = generate_matches_json(matches, today_str, category_name=filter)
    return Response(
        content=bio.getvalue(),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@router.get("/status", response_model=StatusResponse)
async def get_status():
    """System health, uptime, and processing statistics."""
    uptime = (datetime.now(timezone.utc) - _start_time).total_seconds()
    stats = _engine.stats if _engine else {}

    featured_count = (
        len(_settings.featured_leagues)
        if _settings and isinstance(_settings.featured_leagues, list)
        else 0
    )

    return StatusResponse(
        status="healthy" if _engine and _engine._running else "running",
        uptime_seconds=round(uptime, 1),
        stats=stats,
        active_monitor="SofaScoreMonitor (Playwright)",
        featured_leagues_count=featured_count,
    )
