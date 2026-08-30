"""
Tests for head-to-head screening.

The orientation test is the one that matters. A past meeting can have the
fixture reversed, and counting it unoriented inverts the result for roughly
half the sample while still producing a confident-looking number.
"""

import time

import pytest

from core.predictor.filter import Fixture
from core.predictor.h2h import (
    BASE_RATES,
    DEFAULT_MIN_MEETINGS,
    H2HRecord,
    Meeting,
    best_selection,
    build_record,
)

NOW = int(time.time())
DAY = 86400


def _fixture(home_id: int = 100, away_id: int = 200) -> Fixture:
    return Fixture(
        match_id=1, tournament="T", category="C",
        home_name="Home FC", away_name="Away FC",
        home_id=home_id, away_id=away_id,
        start_utc="", start_local="",
    )


def _event(home_id, away_id, hg, ag, days_ago=30):
    return {
        "homeTeam": {"id": home_id}, "awayTeam": {"id": away_id},
        "homeScore": {"current": hg}, "awayScore": {"current": ag},
        "startTimestamp": NOW - days_ago * DAY,
    }


def test_reversed_fixtures_are_reoriented():
    """Today's home team was away in some meetings. It still won them."""
    fx = _fixture()
    events = [
        _event(100, 200, 3, 0, 30),   # home side won at home
        _event(200, 100, 0, 2, 400),  # SAME side won away — must count as a home win
    ]
    record = build_record(fx, events)

    assert record.count == 2
    assert record.hits("1") == 2, "the reversed win was counted for the wrong team"
    assert record.hits("2") == 0


def test_goals_follow_the_team_not_the_slot():
    fx = _fixture()
    record = build_record(fx, [_event(200, 100, 1, 4, 30)])
    meeting = record.meetings[0]
    assert meeting.goals_for_home == 4   # today's home team scored 4, away that day
    assert meeting.goals_for_away == 1


def test_unplayed_and_unrelated_events_are_dropped():
    fx = _fixture()
    events = [
        {"homeTeam": {"id": 100}, "awayTeam": {"id": 200},
         "homeScore": {}, "awayScore": {}, "startTimestamp": NOW - DAY},   # no score
        _event(300, 400, 1, 1, 30),                                        # other teams
        _event(100, 200, 2, 1, 30),                                        # valid
    ]
    assert build_record(fx, events).count == 1


def test_stale_meetings_are_excluded():
    fx = _fixture()
    events = [_event(100, 200, 1, 0, 30), _event(100, 200, 1, 0, 5000)]
    assert build_record(fx, events, max_age_days=2200).count == 1


def test_before_ts_keeps_the_future_out():
    """Backtesting must not see meetings that had not happened yet."""
    fx = _fixture()
    cutoff = NOW - 100 * DAY
    events = [_event(100, 200, 1, 0, 50), _event(100, 200, 2, 0, 200)]
    record = build_record(fx, events, before_ts=cutoff)
    assert record.count == 1
    assert record.meetings[0].timestamp < cutoff


def test_market_counting():
    fx = _fixture()
    record = build_record(fx, [
        _event(100, 200, 2, 1, 10),   # home win, GG, 3 goals -> over 2.5
        _event(100, 200, 0, 0, 20),   # draw, no GG, under
        _event(200, 100, 1, 1, 30),   # draw, GG, under
        _event(200, 100, 3, 0, 40),   # away win (today's away side), no GG, over
    ])
    assert record.count == 4
    assert record.hits("1") == 1
    assert record.hits("X") == 2
    assert record.hits("2") == 1
    assert record.hits("GG") == 2
    assert record.hits("Over 2.5") == 2


def test_thin_records_select_nothing():
    fx = _fixture()
    record = build_record(fx, [_event(100, 200, 2, 1, 10)] * 1)
    assert best_selection(record, min_meetings=DEFAULT_MIN_MEETINGS) is None


def test_small_samples_are_shrunk_toward_the_base_rate():
    """4 from 4 is not 100%, and must not be selected on as if it were."""
    fx = _fixture()
    record = build_record(fx, [_event(100, 200, 1, 1, d * 30) for d in range(1, 5)])
    assert record.raw_rate("GG") == 1.0
    shrunk = record.shrunk_probability("GG")
    assert shrunk < 1.0
    assert shrunk > BASE_RATES["GG"]


def test_a_long_record_beats_a_lucky_short_one():
    """The whole reason selection uses the shrunk figure."""
    short = H2HRecord(fixture=_fixture(), meetings=[
        Meeting(NOW - i * DAY, 1, 1) for i in range(4)          # 4/4 GG
    ])
    long = H2HRecord(fixture=_fixture(), meetings=[
        Meeting(NOW - i * DAY, 1, 1) for i in range(11)          # 11/12 GG
    ] + [Meeting(NOW - 99 * DAY, 1, 0)])

    assert short.raw_rate("GG") > long.raw_rate("GG")
    assert long.shrunk_probability("GG") > short.shrunk_probability("GG")


def test_best_selection_returns_shrunk_and_raw():
    """1-0 six times: only the home-win market hits, so there is no tie."""
    fx = _fixture()
    record = build_record(fx, [_event(100, 200, 1, 0, d * 30) for d in range(1, 7)])
    sel, shrunk, raw = best_selection(record)
    assert sel == "1"
    assert raw == 1.0
    assert shrunk < raw


def test_ties_break_toward_the_higher_base_rate():
    """3-1 makes 1, GG and Over 2.5 all perfect. The tie-break must be
    deterministic, or the same record yields different picks between runs."""
    fx = _fixture()
    record = build_record(fx, [_event(100, 200, 3, 1, d * 30) for d in range(1, 7)])
    first = best_selection(record)
    assert first[0] == "GG"
    for _ in range(5):
        assert best_selection(record)[0] == first[0]
