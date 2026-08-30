#!/usr/bin/env python3
"""
Verify the head-to-head fetch against the live API, cheaply.

The one thing `core/predictor/h2h.py` assumes and cannot prove offline is the
shape of `event/{id}/h2h/events`. This probes a handful of fixtures, prints
what came back RAW, and then prints what the parser made of it — so a mismatch
is visible as a mismatch rather than as an empty result.

Books nothing. Sends nothing. Touches no ticket. Default is 3 fixtures, which
is 3 requests.

    python tools/probe_h2h.py ~/Downloads/sportcrawl_upcoming_2026-08-30.json
    python tools/probe_h2h.py fixtures.json --limit 5
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fixtures", help="fixtures JSON, same file the predictor CLI takes")
    ap.add_argument("--limit", type=int, default=3, help="fixtures to probe (default 3)")
    args = ap.parse_args()

    from core.predictor.filter import filter_fixtures
    from core.predictor.h2h import (
        API_BASE, DEFAULT_MIN_MEETINGS, best_selection, build_record,
    )
    from config.settings import Settings
    from monitors.sofascore_monitor import SofaScoreMonitor

    raw = json.load(open(args.fixtures))
    matches = raw if isinstance(raw, list) else raw.get("matches", raw.get("events", []))
    fixtures, stats = filter_fixtures(matches, allow_unlisted=True)
    if not fixtures:
        print(f"No fixtures survived filtering (of {stats.total}).")
        return 1

    probe = fixtures[: args.limit]
    print(f"\nProbing {len(probe)} of {len(fixtures)} fixtures.\n")

    monitor = SofaScoreMonitor(Settings())
    browser, _ctx, page = await monitor._create_browser_context()
    try:
        await page.goto("https://www.sofascore.com/football",
                        wait_until="domcontentloaded", timeout=25000)
        await asyncio.sleep(1.5)

        for fx in probe:
            print("=" * 70)
            print(f"{fx.label}   (event {fx.match_id}, home_id={fx.home_id})")

            payload = await page.evaluate(
                """async ({id, base}) => {
                    try {
                        const r = await fetch(`${base}/event/${id}/h2h/events`);
                        if (!r.ok) return {__status: r.status};
                        return await r.json();
                    } catch (e) { return {__error: String(e)}; }
                }""",
                {"id": fx.match_id, "base": API_BASE},
            )

            if not isinstance(payload, dict):
                print(f"  unexpected payload type: {type(payload).__name__}")
                continue
            if "__status" in payload:
                print(f"  HTTP {payload['__status']} — endpoint wrong or blocked")
                continue
            if "__error" in payload:
                print(f"  fetch error: {payload['__error']}")
                continue

            print(f"  top-level keys: {sorted(payload.keys())}")
            events = payload.get("events") or []
            print(f"  events returned: {len(events)}")

            if events:
                # The actual shape, which is the whole point of this probe.
                sample = events[0]
                print(f"  first event keys: {sorted(sample.keys())[:14]}")
                print("  fields the parser needs:")
                for path in ("homeTeam.id", "awayTeam.id",
                             "homeScore.current", "awayScore.current", "startTimestamp"):
                    node, missing = sample, False
                    for part in path.split("."):
                        if isinstance(node, dict) and part in node:
                            node = node[part]
                        else:
                            missing = True
                            break
                    print(f"    {path:22s} {'MISSING' if missing else repr(node)}")

            record = build_record(fx, events)
            print(f"  parsed: {record.summary() if record.count else 'NO USABLE MEETINGS'}")
            chosen = best_selection(record)
            if chosen:
                sel, shrunk, rate = chosen
                print(f"  -> would pick {sel}  ({rate:.0%} raw, {shrunk:.0%} shrunk)")
            else:
                print(f"  -> no pick (needs {DEFAULT_MIN_MEETINGS}+ meetings, has {record.count})")
            await asyncio.sleep(0.4)
    finally:
        try:
            await browser.close()
        except Exception:
            pass

    print("=" * 70)
    print("\nIf 'events returned' is 0 everywhere, the endpoint or its shape is wrong.")
    print("If events come back but 'parsed' says NO USABLE MEETINGS, the field")
    print("paths above are the mismatch — send me that block and it is a small fix.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
