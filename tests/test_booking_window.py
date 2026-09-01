"""
Tests for confining bookings to today's card.

A 2-odds banker shipped a leg kicking off the following night. Fixtures were
matched against SportyBet's full upcoming card — ~1,100 events spanning weeks —
on team names alone, with nothing checking that the event resolved to the day
the fixture was screened for.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from services.sportybet_service import (
    BOOKING_TZ,
    booking_horizon,
    within_booking_window,
)

WAT = ZoneInfo("Africa/Lagos")


def _event(kickoff: datetime | None, raw=None):
    if kickoff is None:
        return {"homeTeamName": "H", "awayTeamName": "A", "estimateStartTime": raw}
    return {
        "homeTeamName": "H",
        "awayTeamName": "A",
        "estimateStartTime": int(kickoff.timestamp() * 1000),
    }


@pytest.fixture
def now():
    # 14:00 WAT on 1 Sep 2026 — when the bad ticket was noticed.
    return datetime(2026, 9, 1, 14, 0, tzinfo=WAT).astimezone(timezone.utc)


def test_todays_later_kickoff_is_kept(now):
    """Portsmouth v Derby, 19:45 WAT the same evening."""
    ko = datetime(2026, 9, 1, 19, 45, tzinfo=WAT).astimezone(timezone.utc)
    assert within_booking_window(_event(ko), now) is True


def test_tomorrow_night_is_rejected(now):
    """The leg that caused this: 22:30 WAT the following day."""
    ko = datetime(2026, 9, 2, 22, 30, tzinfo=WAT).astimezone(timezone.utc)
    assert within_booking_window(_event(ko), now) is False


def test_late_tonight_is_still_today(now):
    ko = datetime(2026, 9, 1, 23, 45, tzinfo=WAT).astimezone(timezone.utc)
    assert within_booking_window(_event(ko), now) is True


def test_already_kicked_off_is_rejected(now):
    ko = datetime(2026, 9, 1, 12, 0, tzinfo=WAT).astimezone(timezone.utc)
    assert within_booking_window(_event(ko), now) is False


def test_weeks_away_is_rejected(now):
    ko = datetime(2026, 9, 16, 17, 45, tzinfo=WAT).astimezone(timezone.utc)
    assert within_booking_window(_event(ko), now) is False


@pytest.mark.parametrize("raw", [None, "", "not-a-number", {}])
def test_unusable_start_time_is_kept(raw, now):
    """Never shrink the card because SportyBet changed a field."""
    assert within_booking_window(_event(None, raw=raw), now) is True


def test_horizon_defaults_to_end_of_today_in_wat(now):
    end = booking_horizon(now).astimezone(BOOKING_TZ)
    assert end.date() == datetime(2026, 9, 1).date()
    assert (end.hour, end.minute) == (23, 59)


def test_horizon_env_override_uses_a_rolling_window(now, monkeypatch):
    monkeypatch.setenv("BOOKING_HORIZON_HOURS", "6")
    assert booking_horizon(now) == now + timedelta(hours=6)
    ko = datetime(2026, 9, 1, 23, 45, tzinfo=WAT).astimezone(timezone.utc)
    assert within_booking_window(_event(ko), now) is False  # beyond 6h


def test_bad_env_value_falls_back_to_end_of_day(now, monkeypatch):
    monkeypatch.setenv("BOOKING_HORIZON_HOURS", "banana")
    assert booking_horizon(now).astimezone(BOOKING_TZ).hour == 23
