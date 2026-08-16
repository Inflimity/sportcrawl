"""
Unit and integration tests for SofaScore Football Bot components.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import pytest
import pytest_asyncio

from config.settings import Settings
from monitors.sofascore_monitor import SofaScoreMonitor
from notifiers.telegram_bot import _format_match_row, format_matches_message
from storage.database import DatabaseManager
from storage.models import FootballMatch


@pytest.fixture
def settings():
    return Settings(
        telegram_bot_token="123456:dummy_token",
        admin_chat_id=12345678,
        database_url="sqlite+aiosqlite:///:memory:",
        featured_leagues=["Premier League", "LaLiga", "UEFA Champions League"],
    )


@pytest.fixture
def monitor(settings):
    return SofaScoreMonitor(settings)


@pytest_asyncio.fixture
async def db():
    manager = DatabaseManager("sqlite+aiosqlite:///:memory:")
    await manager.init_db()
    yield manager
    await manager.close()


def test_normalize_event(monitor):
    """Test SofaScore raw event normalization into clean fixture dict."""
    raw_event = {
        "id": 123456,
        "slug": "arsenal-manchester-city",
        "customId": "abc1234",
        "tournament": {
            "name": "Premier League",
            "category": {"name": "England"},
        },
        "roundInfo": {"name": "Round 1"},
        "homeTeam": {"id": 42, "name": "Arsenal"},
        "awayTeam": {"id": 17, "name": "Manchester City"},
        "startTimestamp": 1750000000,
        "status": {"type": "inprogress", "description": "1st half"},
        "homeScore": {"current": 1, "period1": 1},
        "awayScore": {"current": 0, "period1": 0},
        "time": {"played": "35"},
    }

    norm = monitor._normalize_event(raw_event)
    assert norm is not None
    assert norm["match_id"] == 123456
    assert norm["home_team"] == "Arsenal"
    assert norm["away_team"] == "Manchester City"
    assert norm["tournament_name"] == "Premier League"
    assert norm["category_name"] == "England"
    assert norm["is_featured"] is True
    assert norm["home_score"] == 1
    assert norm["away_score"] == 0
    assert norm["minute"] == "35"
    assert "https://www.sofascore.com/arsenal-manchester-city/abc1234" == norm["sofascore_url"]


@pytest.mark.asyncio
async def test_database_upsert_and_goal_detection(db):
    """Test match upserting, goal change detection, and querying."""
    now_ts = int(datetime.now(timezone.utc).timestamp())
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    match_data_1 = {
        "match_id": 9991,
        "slug": "barcelona-real-madrid",
        "tournament_name": "LaLiga",
        "category_name": "Spain",
        "round_info": "Round 10",
        "is_featured": True,
        "home_team": "Barcelona",
        "away_team": "Real Madrid",
        "start_timestamp": now_ts,
        "start_time": datetime.now(timezone.utc),
        "match_date": today_str,
        "status_type": "inprogress",
        "status_description": "1st half",
        "home_score": 0,
        "away_score": 0,
        "minute": "15",
        "sofascore_url": "https://www.sofascore.com/barcelona-real-madrid/9991",
        "raw": {},
    }

    # First insert: initial 0-0
    match_obj, is_new, score_change = await db.upsert_match(match_data_1, is_featured=True)
    assert is_new is True
    assert score_change is None
    assert match_obj.home_team == "Barcelona"
    assert match_obj.home_score == 0

    # Second insert: Goal scored! 1-0
    match_data_1["home_score"] = 1
    match_data_1["minute"] = "32"
    match_obj, is_new, score_change = await db.upsert_match(match_data_1, is_featured=True)
    assert is_new is False
    assert score_change == "GOAL! Barcelona 1 - 0 Real Madrid"
    assert match_obj.home_score == 1

    # Query matches for date
    matches = await db.get_matches_for_date(today_str)
    assert len(matches) == 1
    assert matches[0].match_id == 9991

    # Query live matches
    live = await db.get_live_matches()
    assert len(live) == 1
    assert live[0].match_id == 9991

    # Toggle bookmark
    is_bookmarked = await db.toggle_bookmark(9991, chat_id=123456)
    assert is_bookmarked is True

    bookmarked_list = await db.get_bookmarked_matches()
    assert len(bookmarked_list) == 1


def test_telegram_message_formatting():
    """Test formatting match rows and Telegram message chunking."""
    now = datetime.now(timezone.utc)
    match = FootballMatch(
        id=1,
        match_id=888,
        slug="liverpool-chelsea",
        tournament_name="Premier League",
        category_name="England",
        round_info="",
        is_featured=True,
        home_team="Liverpool",
        away_team="Chelsea",
        start_timestamp=int(now.timestamp()),
        start_time=now,
        match_date=now.strftime("%Y-%m-%d"),
        status_type="inprogress",
        status_description="2nd half",
        home_score=2,
        away_score=1,
        minute="65",
        sofascore_url="https://www.sofascore.com/liverpool-chelsea/888",
    )

    row = _format_match_row(match)
    assert "Liverpool vs Chelsea" in row
    assert "2-1" in row
    assert "65" in row

    chunks = format_matches_message([match], "Today's Matches")
    assert len(chunks) == 1
    assert "Premier League" in chunks[0]
    assert "Liverpool" in chunks[0]
