"""
Grade the ticket log: did the legs beat the price they were bought at?

``core.ticket_log`` records every leg with its price at booking time. This
reads that log, fetches the real outcomes from SofaScore, and answers the one
question the backtest structurally cannot:

    the backtest measures hit rate against a BASE RATE.
    this measures hit rate against the PRICE ACTUALLY PAID.

Those are different questions. A market can beat its base rate by 4 points and
still lose money, if the bookmaker was already charging for 5. Only the price
decides profit, and only this log has the price.

Outcomes come from SofaScore team histories, never from the local database —
the database never records ~39% of results and the gap skews toward low-scoring
competitions, which silently inflates every goals market.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.predictor.filter import Fixture  # noqa: E402
from tools.backtest_markets import Tally, grade, outcomes_from_events  # noqa: E402


def load_log(path: str, ticket: Optional[str] = None) -> list[dict[str, Any]]:
    rows = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ticket and row.get("ticket") != ticket:
            continue
        rows.append(row)
    return rows


def _fixture_from(row: dict[str, Any], home_id: int, away_id: int) -> Fixture:
    return Fixture(
        match_id=int(row["match_id"]), tournament=row.get("tournament") or "",
        category="", home_name=row.get("home_name") or "",
        away_name=row.get("away_name") or "",
        home_id=home_id, away_id=away_id,
        start_utc=row.get("kickoff_utc") or "", start_local="",
    )


async def fetch_outcomes(
    rows: list[dict[str, Any]], cache_path: Optional[str] = None
) -> dict[int, tuple[int, int]]:
    """
    Resolve every logged fixture's final score, fetching as little as possible.

    Three economies, because this is run repeatedly against a log that only
    grows and SofaScore throttles hard under load:

    1. **Settled results are cached to disk.** A finished match never changes,
       so it is fetched once and never again. Without this the cost of a report
       grows with the age of the log forever.
    2. **One team per fixture, not two.** A fixture appears in *either*
       participant's history, so the home side alone answers it. Only the
       fixtures still missing afterwards are retried against the away side.
    3. **Unplayed fixtures are skipped**, rather than fetched to discover they
       have no score yet.
    """
    from core.predictor.enrich import fetch_team_forms

    cache: dict[int, tuple[int, int]] = {}
    cpath = Path(cache_path) if cache_path else None
    if cpath and cpath.exists():
        try:
            cache = {int(k): tuple(v) for k, v in json.loads(cpath.read_text()).items()}
        except Exception:
            cache = {}

    now = datetime.now(timezone.utc)
    pending: dict[int, dict[str, Any]] = {}
    for r in rows:
        mid = r.get("match_id")
        if mid is None or int(mid) in cache or int(mid) in pending:
            continue
        ko = r.get("kickoff_utc")
        if ko:
            try:
                kt = datetime.fromisoformat(str(ko).replace("Z", "+00:00"))
                if kt.tzinfo is None:
                    kt = kt.replace(tzinfo=timezone.utc)
                if kt > now:
                    continue  # not played yet; nothing to fetch
            except ValueError:
                pass
        pending[int(mid)] = r

    found: dict[int, tuple[int, int]] = {}
    if pending:
        for attempt, side in enumerate(("home_id", "away_id")):
            todo = [r for mid, r in pending.items() if mid not in found]
            if not todo:
                break
            fixtures = []
            for r in todo:
                tid = int(r.get(side) or 0)
                if tid:
                    # Both ids set to the one side being asked, so the fetch
                    # covers one team per fixture rather than two.
                    fixtures.append(_fixture_from(r, tid, tid))
            if not fixtures:
                continue
            raw: dict[int, list[dict[str, Any]]] = {}
            await fetch_team_forms(fixtures, form_matches=1, raw_out=raw)
            found.update(outcomes_from_events(raw))

    cache.update({k: v for k, v in found.items()})
    if cpath:
        try:
            cpath.parent.mkdir(parents=True, exist_ok=True)
            cpath.write_text(json.dumps({str(k): list(v) for k, v in cache.items()}))
        except Exception as e:  # noqa: BLE001
            print(f"(could not write outcome cache: {e})", file=sys.stderr)
    return cache


def report(rows: list[dict[str, Any]], scores: dict[int, tuple[int, int]]) -> str:
    by_market: dict[str, Tally] = defaultdict(Tally)
    prices: dict[str, list[float]] = defaultdict(list)
    staked = returned = 0.0
    ungraded = 0
    tickets: dict[tuple, list[dict[str, Any]]] = defaultdict(list)

    for r in rows:
        actual = scores.get(r.get("match_id"))
        sel = r.get("selection")
        if not actual or not sel:
            ungraded += 1
            continue
        won = grade(sel, *actual)
        if won is None:
            ungraded += 1
            continue
        by_market[sel].add(won)
        if isinstance(r.get("price"), (int, float)):
            prices[sel].append(r["price"])
        r["_won"] = won
        tickets[(r.get("logged_at"), r.get("ticket"))].append(r)

    out = ["=" * 74, f"TICKET LOG — {len(rows)} legs logged, {ungraded} not yet gradable", "=" * 74, ""]
    out.append(f"{'selection':<12}{'legs':>6}{'hit':>9}{'avg price':>11}{'implied':>10}{'edge':>10}")
    out.append("-" * 74)
    for sel, t in sorted(by_market.items(), key=lambda kv: -kv[1].total):
        p = prices.get(sel) or []
        if p:
            avg = statistics.mean(p)
            imp = 1 / avg
            out.append(f"{sel:<12}{t.total:>6}{t.rate:>8.1%}{avg:>11.2f}{imp:>9.1%}"
                       f"{(t.rate - imp) * 100:>+9.1f}pp")
        else:
            out.append(f"{sel:<12}{t.total:>6}{t.rate:>8.1%}{'—':>11}{'—':>10}{'—':>10}")

    # Ticket-level money, the thing you actually staked.
    complete = 0
    for (_stamp, name), legs in tickets.items():
        if len(legs) != legs[0].get("ticket_legs"):
            continue  # a leg is still ungraded; the ticket is undecided
        complete += 1
        staked += 1.0
        if all(leg["_won"] for leg in legs):
            returned += legs[0].get("ticket_odds") or 0.0

    if complete:
        out += ["", f"Tickets fully settled: {complete}",
                f"  staked {complete} units, returned {returned:.2f} units",
                f"  profit {returned - staked:+.2f} units "
                f"({(returned / staked - 1) * 100:+.1f}%)"]
        out.append(f"  on N10,000 a ticket: N{(returned - staked) * 10_000:+,.0f}")

    out += ["", "'edge' is hit rate minus what the price implies. That is the only",
            "number that decides profit — beating the base rate is not enough if",
            "the bookmaker already charged for the difference."]
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default="logs/tickets.jsonl")
    ap.add_argument("--ticket", help="only this ticket, e.g. two_odds")
    ap.add_argument("--cache", default="logs/outcomes.json",
                    help="settled results, so a finished match is fetched once ever")
    args = ap.parse_args()

    rows = load_log(args.log, args.ticket)
    if not rows:
        print(f"No rows in {args.log}"
              f"{' for ticket ' + args.ticket if args.ticket else ''}.")
        return
    scores = asyncio.run(fetch_outcomes(rows, args.cache))
    print(report(rows, scores))


if __name__ == "__main__":
    main()
