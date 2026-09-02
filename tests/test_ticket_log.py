"""
Tests for the price log.

SportyBet keeps no odds history, so a price not captured at booking time is
gone for good. These pin the two properties that matter: the price is actually
recorded, and a logging fault can never break a prediction run.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from core.ticket_log import log_result, log_ticket


def _fixture(i=1):
    return SimpleNamespace(match_id=1000 + i, home_id=10 + i, away_id=20 + i,
                           home_name=f"Home {i}", away_name=f"Away {i}",
                           tournament="Championship", start_utc="2026-09-02T18:45:00")


def _leg(i=1, sel="Over 1.5", prob=0.91):
    return SimpleNamespace(fixture=_fixture(i), selection=sel, market="Total Goals",
                           probability=prob, rationale="last 5: WWDLW / LDLLL")


def _ticket(n=2):
    return SimpleNamespace(legs=[_leg(i) for i in range(1, n + 1)],
                           leg_odds=[1.33, 1.30][:n], combined_odds=1.73,
                           combined_probability=0.81)


def _read(p):
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def test_logs_one_row_per_leg_with_the_price(tmp_path):
    f = tmp_path / "t.jsonl"
    n = log_ticket("two_odds", _ticket(2),
                   SimpleNamespace(success=True, booking_code="TZ48SK"), str(f))
    assert n == 2
    rows = _read(f)
    assert [r["price"] for r in rows] == [1.33, 1.30]        # the perishable half
    assert [r["selection"] for r in rows] == ["Over 1.5", "Over 1.5"]
    assert rows[0]["booking_code"] == "TZ48SK" and rows[0]["booked"] is True
    assert rows[0]["match_id"] == 1001 and rows[0]["home_id"] == 11
    assert rows[0]["ticket_odds"] == 1.73


def test_appends_rather_than_overwrites(tmp_path):
    f = tmp_path / "t.jsonl"
    log_ticket("two_odds", _ticket(2), None, str(f))
    log_ticket("two_odds", _ticket(2), None, str(f))
    assert len(_read(f)) == 4


def test_unbooked_ticket_still_records_the_price(tmp_path):
    """A code that failed to generate does not make the price worthless."""
    f = tmp_path / "t.jsonl"
    log_ticket("two_odds", _ticket(2),
               SimpleNamespace(success=False, booking_code=None), str(f))
    rows = _read(f)
    assert rows[0]["booked"] is False and rows[0]["price"] == 1.33


def test_empty_or_missing_ticket_is_a_no_op(tmp_path):
    f = tmp_path / "t.jsonl"
    assert log_ticket("two_odds", None, None, str(f)) == 0
    assert log_ticket("two_odds", SimpleNamespace(legs=[]), None, str(f)) == 0
    assert not f.exists()


def test_a_logging_fault_never_raises(tmp_path):
    """A prediction must never fail because a log file was unwritable."""
    bad = tmp_path / "nope"
    bad.write_text("i am a file, not a directory")
    assert log_ticket("two_odds", _ticket(2), None, str(bad / "t.jsonl")) == 0


def test_log_result_covers_every_ticket(tmp_path):
    f = tmp_path / "t.jsonl"
    res = SimpleNamespace(
        tier_10=SimpleNamespace(picks=[_leg(1)], booking_result=None),
        tier_20=SimpleNamespace(picks=[_leg(2)], booking_result=None),
        two_odds=SimpleNamespace(ticket=_ticket(2),
                                 booking_result=SimpleNamespace(success=True, booking_code="A1")),
        draws=SimpleNamespace(tickets=[SimpleNamespace(
            ticket=SimpleNamespace(legs=[_leg(3, "X", 0.31)], leg_odds=[3.4],
                                   combined_odds=3.4, combined_probability=0.31,
                                   label="5-fold"),
            booking_result=None)]),
    )
    assert log_result(res, str(f)) == 5
    assert {r["ticket"] for r in _read(f)} == {"top_10", "top_20", "two_odds", "draw_5-fold"}


# --- ledger fetch economy -------------------------------------------------
# The ledger is run repeatedly against a log that only grows. Without caching,
# the cost of a report would grow with the age of the log forever, and
# SofaScore throttles hard under sustained load.

def _log_row(match_id, kickoff, home_id=11, away_id=21):
    return {"match_id": match_id, "home_id": home_id, "away_id": away_id,
            "home_name": "H", "away_name": "A", "tournament": "T",
            "kickoff_utc": kickoff, "selection": "Over 1.5", "price": 1.30,
            "ticket": "two_odds", "ticket_legs": 1, "ticket_odds": 1.30,
            "logged_at": "2026-09-01T08:00:00+00:00"}


def test_settled_results_are_never_refetched(tmp_path, monkeypatch):
    import asyncio
    from tools import ticket_ledger

    cache = tmp_path / "outcomes.json"
    cache.write_text('{"555": [2, 1]}')
    calls = []

    async def _boom(*a, **k):
        calls.append(1)
        raise AssertionError("should not fetch a match already settled")

    monkeypatch.setattr("core.predictor.enrich.fetch_team_forms", _boom)
    rows = [_log_row(555, "2026-09-01T18:45:00")]
    out = asyncio.run(ticket_ledger.fetch_outcomes(rows, str(cache)))
    assert out == {555: (2, 1)} and not calls


def test_unplayed_fixtures_are_not_fetched(tmp_path, monkeypatch):
    """No point asking for a score that cannot exist yet."""
    import asyncio
    from tools import ticket_ledger

    calls = []

    async def _boom(*a, **k):
        calls.append(1)
        raise AssertionError("should not fetch a fixture that has not kicked off")

    monkeypatch.setattr("core.predictor.enrich.fetch_team_forms", _boom)
    rows = [_log_row(777, "2099-01-01T18:45:00")]
    out = asyncio.run(ticket_ledger.fetch_outcomes(rows, str(tmp_path / "c.json")))
    assert out == {} and not calls


def test_one_team_per_fixture_not_two(tmp_path, monkeypatch):
    """A fixture appears in either side's history; asking both doubles the load."""
    import asyncio
    from tools import ticket_ledger

    seen_team_ids = []

    async def _fake(fixtures, form_matches=10, raw_out=None, **k):
        for fx in fixtures:
            seen_team_ids.append((fx.home_id, fx.away_id))
        if raw_out is not None:
            raw_out[11] = [{"id": 555, "status": {"type": "finished"},
                            "homeScore": {"current": 2}, "awayScore": {"current": 0}}]
        return {}

    monkeypatch.setattr("core.predictor.enrich.fetch_team_forms", _fake)
    rows = [_log_row(555, "2026-09-01T18:45:00")]
    out = asyncio.run(ticket_ledger.fetch_outcomes(rows, str(tmp_path / "c.json")))
    assert out[555] == (2, 0)
    assert seen_team_ids == [(11, 11)]   # home side only, asked once
