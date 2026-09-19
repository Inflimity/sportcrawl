"""
Rank the LEGS available in a fixture — by what they cost, not by what they pay.

WHY RANK BY MARGIN AND NOT BY EDGE
----------------------------------
The obvious tool is one that scores every market and shows the best few. That
tool is a lie, and ANALYSIS.md §2 measured exactly how big a lie: scoring 210
corner lines gave a mean edge of -0.40% and a median of -0.70%, yet taking the
best line per fixture produced "+8.0%" on 21 of 21 fixtures. Per-line noise had
a 7.4% standard deviation and we were reporting its maximum.

So this ranks by things that are MEASURED rather than estimated:

  margin      computed from the book's own two-sided prices. No model, no
              noise, no season averages. A 3.5% leg costs less than a 7.8% leg,
              and that is true regardless of who wins.

  redundancy  a leg logically implied by another leg carries no risk and should
              add no odds. "Yamal to score or assist" is implied by "Yamal 1+
              goal": if he scores, the first is automatically true. Nesting is
              a fact about the markets, not a prediction.

  correlation from measured co-occurrence, where history exists.

What it will NOT do is tell you which leg wins. It tells you which legs are
cheap, which are free, and which are secretly the same bet.

USAGE
-----
    python3 -m tools.builder_legs --tournament "UEFA Champions League"
    python3 -m tools.builder_legs "Barcelona v Feyenoord" "Napoli v Arsenal"
    python3 -m tools.builder_legs --tournament "UEFA Champions League" --max-margin 5.0
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent / "sportybet-skill"))

from tools.builder_price import strip_margin, match_fixture, _is_youth  # noqa: E402

# Markets worth putting in a builder, by id. Anything with a huge outcome list
# (player props, correct score, scorer markets) is excluded from the margin
# ranking: margin across a 60-name list is not comparable to a two-way market's,
# and reporting them side by side would be comparing different quantities.
TWO_WAY_ONLY = True

# Leg pairs where the first LOGICALLY IMPLIES the second, so the second adds no
# risk once the first is in the slip. Facts about the markets, not predictions.
NESTED: list[tuple[str, str, str]] = [
    ("<player> 1+ goal", "<player> to score or assist",
     "scoring makes 'score or assist' automatically true"),
    ("<player> 2+ goals", "<player> 1+ goal",
     "two goals implies one"),
    ("Home to Win Both Halves", "Home win / Home or Draw",
     "winning both halves implies winning the match"),
    ("Correct Score", "anything about goals",
     "an exact score fixes every goals market in the fixture"),
    ("Over 3.5", "Over 2.5 / Over 1.5 / Over 0.5",
     "a higher over implies every lower one"),
    ("Home Team Over 2.5 (team goals)", "Over 2.5 match goals",
     "one team scoring 3 guarantees the match goes over 2.5"),
]


def _describe(market: dict[str, Any]) -> str:
    d = market.get("desc") or market.get("name") or "?"
    spec = market.get("specifier") or ""
    if spec and "variant" not in spec:
        d = f"{d} [{spec}]"
    return d


async def rank_fixture(svc, event: dict[str, Any], max_margin: Optional[float], top: int) -> None:
    label = f"{event.get('homeTeamName')} v {event.get('awayTeamName')}"
    markets = await svc.fetch_event_markets(event.get("eventId"))

    rows: list[tuple[float, str, str, float]] = []
    for m in markets:
        outs = m.get("outcomes") or []
        if TWO_WAY_ONLY and not (2 <= len(outs) <= 3):
            continue
        if "variant" in (m.get("specifier") or ""):
            continue
        probs, margin = strip_margin(outs)
        if not probs or margin <= 0 or margin > 0.60:
            continue
        for o in outs:
            desc = o.get("desc", "")
            if desc not in probs:
                continue
            try:
                odds = float(o.get("odds") or 0)
            except (TypeError, ValueError):
                continue
            if odds <= 1.01:            # no room to be worth booking
                continue
            rows.append((margin, _describe(m), desc, odds))

    # De-duplicate: SportyBet lists most markets twice (a prematch copy and a
    # live copy). Keep the cheapest sighting of each market+outcome.
    best: dict[tuple[str, str], tuple[float, float]] = {}
    for margin, mname, desc, odds in rows:
        key = (mname, desc)
        if key not in best or margin < best[key][0]:
            best[key] = (margin, odds)

    ranked = sorted(((m, o, k[0], k[1]) for k, (m, o) in best.items()), key=lambda r: r[0])
    if max_margin is not None:
        ranked = [r for r in ranked if r[0] <= max_margin / 100.0]

    print(f"\n── {label} ──")
    if not ranked:
        print("   no two-way markets returned")
        return
    print(f"   {len(best)} two-way legs available   ·   cheapest {ranked[0][0]*100:.1f}% "
          f"·   median {sorted(r[0] for r in ranked)[len(ranked)//2]*100:.1f}%")
    print(f"   {'margin':>7}  {'odds':>6}  leg")
    for margin, odds, mname, desc in ranked[:top]:
        print(f"   {margin*100:6.1f}%  {odds:6.2f}  {desc} — {mname}")


async def main() -> None:
    ap = argparse.ArgumentParser(description="Rank a fixture's builder legs by cost.")
    ap.add_argument("fixtures", nargs="*")
    ap.add_argument("--tournament", help='e.g. "UEFA Champions League"')
    ap.add_argument("--max-margin", type=float, default=None, help="only legs at or below this %%")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--pages", type=int, default=12)
    args = ap.parse_args()

    from sportybet.service import SportyBetBookerService

    svc = SportyBetBookerService()
    events = await svc.fetch_available_events(max_pages=args.pages)

    targets: list[dict[str, Any]] = []
    if args.tournament:
        want = args.tournament.lower()
        for e in events:
            name = (e.get("sport", {}).get("category", {}).get("tournament", {}).get("name") or "")
            if want in name.lower() and not _is_youth(e):
                targets.append(e)
    for q in args.fixtures:
        ev = match_fixture(q, events)
        if ev:
            targets.append(ev)

    if not targets:
        print("No fixtures matched.", file=sys.stderr)
        return

    print(f"{len(targets)} fixture(s)   ·   legs ranked by MARGIN, the only quantity here "
          f"that is measured rather than estimated")

    for ev in targets:
        await rank_fixture(svc, ev, args.max_margin, args.top)

    print("\n" + "─" * 76)
    print("Legs that are logically nested — the second adds no risk once you have the first:")
    for a, b, why in NESTED:
        print(f"   {a:<34} ⊃ {b:<34}")
        print(f"   {'':34}   {why}")
    print("\nThis ranks what legs COST. It does not predict which win — scoring many")
    print("candidates and showing the best is how ANALYSIS.md §2 produced '+8% edge'")
    print("on 21 of 21 fixtures from data whose true mean was -0.4%.")


if __name__ == "__main__":
    asyncio.run(main())
