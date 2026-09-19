"""
Integration guard for the Top 20 extra-markets path in run_dual_pipeline.

WHY THIS EXISTS
---------------
The unit tests for `extra_markets` and `build_mixed_card` all passed while
`run_dual_pipeline` was broken in production. The bug was a one-line variable
shadow: the extra-markets block bound `stats = await ensure_stats(...)` over
the `stats` that `filter_fixtures` had returned, so `DualPipelineResult` was
handed a dict as its `filter_stats`. It surfaced only when the digest read
`.total` off it — AFTER the tickets had been booked, so the slips went out and
the message failed.

No unit test could catch that, because every unit was correct. This exercises
the assembled function with the network edges stubbed.
"""

from __future__ import annotations

import pytest

from core.predictor.enrich import TeamForm
from core.predictor.filter import FilterStats
from core.predictor.screen import Pick
from services.pipeline import PredictionBookingPipeline


def _raw(i: int) -> dict:
    return {
        "match_id": 9000 + i,
        "tournament": "LaLiga",
        "category": "Spain",
        "home_team": {"id": 700 + i * 2, "name": f"Home{i}"},
        "away_team": {"id": 701 + i * 2, "name": f"Away{i}"},
        "status_type": "notstarted",
        "start_time_utc": "2099-01-01T18:00:00",
        "start_time_wat": "2099-01-01 19:00:00",
    }


@pytest.fixture
def stubbed(monkeypatch):
    """Stub every network edge: SofaScore forms, match stats, SportyBet."""
    import services.pipeline as mod

    async def fake_forms(fixtures, raw_out=None, **kw):
        forms = {}
        for fx in fixtures:
            for tid, nm in ((fx.home_id, fx.home_name), (fx.away_id, fx.away_name)):
                forms[tid] = TeamForm(
                    team_id=tid, name=nm, matches_used=10, gf_avg=1.9, ga_avg=1.1,
                    over15_rate=0.85, over25_rate=0.62, btts_rate=0.6,
                    scored_rate=0.9, clean_sheet_rate=0.3, recent_results="WWDWL",
                )
                if raw_out is not None:
                    raw_out[tid] = [
                        {"id": 5000 + tid * 10 + n, "status": {"type": "finished"},
                         "homeTeam": {"id": tid}, "awayTeam": {"id": 999}}
                        for n in range(10)
                    ]
        return forms

    monkeypatch.setattr(mod, "fetch_team_forms", fake_forms)

    async def fake_ensure_stats(raw_by_team, **kw):
        out = {}
        for events in raw_by_team.values():
            for ev in events:
                out[ev["id"]] = {
                    "match_id": ev["id"], "home_corners": 6, "away_corners": 5,
                    "home_shots_on_target": 5, "away_shots_on_target": 4,
                }
        return out

    monkeypatch.setattr(
        "core.predictor.stats_form.ensure_stats", fake_ensure_stats, raising=True
    )

    pipe = PredictionBookingPipeline(country_code="ng", headless=True)

    async def fake_price(picks, depth=30):
        from core.predictor.odds import PricedPick
        return [PricedPick(pick=p, odds=1.60) for p in picks[:depth]]

    monkeypatch.setattr(pipe, "_price_shortlist", fake_price)
    return pipe


@pytest.mark.asyncio
async def test_filter_stats_survives_the_extra_markets_block(stubbed):
    """The regression: `filter_stats` must still be a FilterStats afterwards."""
    result = await stubbed.run_dual_pipeline(
        [_raw(i) for i in range(12)],
        auto_book=False,
        top_extra_markets=True,
    )
    assert isinstance(result.filter_stats, FilterStats), (
        f"filter_stats came back as {type(result.filter_stats).__name__} — "
        "something in the pipeline rebound it"
    )
    # The exact access that failed in production.
    assert isinstance(result.filter_stats.total, int)


@pytest.mark.asyncio
async def test_the_digest_that_actually_crashed_renders(stubbed):
    """`format_telegram_dual_digest` reads `.total`; booking happens before it,
    so a failure here ships slips with no message."""
    result = await stubbed.run_dual_pipeline(
        [_raw(i) for i in range(12)],
        auto_book=False,
        top_extra_markets=True,
    )
    text = PredictionBookingPipeline.format_telegram_dual_digest(result, "2026-09-19")
    assert "Screened" in text
    assert "TICKET 2" in text


@pytest.mark.asyncio
async def test_to_dict_also_survives(stubbed):
    """`to_dict` reads `filter_stats.total` too — same crash, different caller."""
    result = await stubbed.run_dual_pipeline(
        [_raw(i) for i in range(12)], auto_book=False, top_extra_markets=True,
    )
    assert result.tier_20.to_dict()["total_screened"] >= 0


@pytest.mark.asyncio
async def test_extras_off_is_unaffected(stubbed):
    result = await stubbed.run_dual_pipeline(
        [_raw(i) for i in range(12)], auto_book=False, top_extra_markets=False,
    )
    assert isinstance(result.filter_stats, FilterStats)
    assert all(p.validated for p in result.tier_20.picks)
