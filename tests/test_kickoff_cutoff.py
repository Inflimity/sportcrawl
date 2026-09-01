"""
Tests for the kickoff cutoff in fixture filtering.

The reported failure: a 21:00 /predict returned a selection on a match that
kicked off at 15:00 and had already finished. ``status_type`` did not catch it
because the upcoming-fixtures sweep never revisits a match once it starts, so
the stored row still read "notstarted" six hours after full time. A status
written by the last scrape can go stale; the clock cannot.
"""

from datetime import datetime, timedelta, timezone

from core.predictor.filter import filter_fixtures, kickoff_utc

NOW = datetime(2026, 9, 1, 21, 0, tzinfo=timezone.utc)


def _match(mid: int, kickoff: datetime, status: str = "notstarted",
           with_timestamp: bool = True) -> dict:
    match = {
        "match_id": mid,
        "tournament": "Premier League",
        "category": "England",
        "home_team": {"id": mid * 10, "name": f"Home {mid}"},
        "away_team": {"id": mid * 10 + 1, "name": f"Away {mid}"},
        "status_type": status,
        "start_time_utc": kickoff.isoformat(),
    }
    if with_timestamp:
        match["startTimestamp"] = int(kickoff.timestamp())
    return match


def _kept(raw, **kwargs):
    fixtures, stats = filter_fixtures(raw, allow_unlisted=True, now=NOW, **kwargs)
    return [f.match_id for f in fixtures], stats


def test_a_played_match_still_marked_notstarted_is_dropped():
    """The exact reported case: 15:00 kickoff, screened at 21:00."""
    kept, stats = _kept([_match(1, NOW - timedelta(hours=6))])
    assert kept == []
    assert stats.past_kickoff == 1


def test_an_upcoming_match_survives():
    kept, _ = _kept([_match(1, NOW + timedelta(hours=3))])
    assert kept == [1]


def test_a_match_inside_the_lead_time_is_dropped():
    """SportyBet closes the market at kickoff; screening one two minutes out is useless."""
    kept, _ = _kept([_match(1, NOW + timedelta(minutes=2))])
    assert kept == []


def test_the_lead_time_is_configurable():
    raw = [_match(1, NOW + timedelta(minutes=20))]
    assert _kept(raw, min_lead_minutes=5)[0] == [1]
    assert _kept(raw, min_lead_minutes=60)[0] == []


def test_falls_back_to_the_iso_timestamp():
    """Not every source carries startTimestamp; start_time_utc must still work."""
    kept, stats = _kept([_match(1, NOW - timedelta(hours=4), with_timestamp=False)])
    assert kept == []
    assert stats.past_kickoff == 1


def test_a_naive_iso_timestamp_is_read_as_utc():
    raw = [_match(1, NOW - timedelta(hours=4), with_timestamp=False)]
    raw[0]["start_time_utc"] = (NOW - timedelta(hours=4)).replace(tzinfo=None).isoformat()
    assert _kept(raw)[0] == []


def test_a_fixture_with_no_kickoff_is_not_dropped_on_time():
    """Unknown is not the same as past — the status check still applies."""
    raw = [_match(1, NOW + timedelta(hours=3))]
    raw[0].pop("startTimestamp")
    raw[0]["start_time_utc"] = ""
    assert _kept(raw)[0] == [1]


def test_status_and_kickoff_are_counted_separately():
    """A finished match and a stale-status match are different diagnoses."""
    _, stats = _kept([
        _match(1, NOW + timedelta(hours=3), status="finished"),
        _match(2, NOW - timedelta(hours=6)),
        _match(3, NOW + timedelta(hours=3)),
    ])
    assert stats.already_started == 1
    assert stats.past_kickoff == 1
    assert stats.kept == 1


def test_kickoff_utc_prefers_the_epoch_field():
    """When the two disagree, the unambiguous one wins."""
    match = _match(1, NOW)
    match["start_time_utc"] = "1999-01-01T00:00:00+00:00"
    assert kickoff_utc(match) == NOW


def test_a_malformed_kickoff_does_not_raise():
    match = _match(1, NOW + timedelta(hours=3), with_timestamp=False)
    match["start_time_utc"] = "not a date"
    assert kickoff_utc(match) is None
    assert _kept([match])[0] == [1]
