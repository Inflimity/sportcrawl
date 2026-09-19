"""
Price bet builders on tonight's SportyBet card.

WHAT THIS IS FOR
----------------
You name some fixtures. This fetches their live SportyBet odds, strips the
margin off both sides, and prices every two-leg combination using how often
those outcomes ACTUALLY co-occur — not by multiplying them.

Multiplying is the mistake a bet builder is designed to profit from, and it errs
in both directions:

    HOME + HT_HOME    naive 16.1%   actual 29.2%   you are 1.8x too pessimistic
    U2.5 + BTTS       naive 20.5%   actual  7.5%   you are 2.7x too optimistic

The output's last column, ``need >``, is the whole decision: the price
SportyBet's builder must beat for the combination to be worth staking once its
margin is accounted for.

WHAT IT WILL NOT DO
-------------------
It will not pick a bet. It is a price checker: it rejects combinations priced
against you. Most of them are.

It also deliberately reports the AVERAGE gap across everything it looked at, not
just the best one. ANALYSIS.md §2 is the reason — screening many candidates and
presenting the maximum manufactured "+8% edge on 21 of 21 fixtures" out of data
whose true mean was -0.4%. If the average here is around zero, the list is a
ranking of noise however good the top row looks.

THE ONE STEP THIS CANNOT DO
---------------------------
SportyBet prices an assembled builder on a different endpoint from the single
markets this reads. So this gives you the fair price and the bar; you read the
app's actual builder offer and compare it yourself.

USAGE
-----
    python3 -m tools.builder_price --db backtest.sqlite "Arsenal v Chelsea" "Real Madrid v Bayern"
    python3 -m tools.builder_price --db backtest.sqlite --card        # whole card
    python3 -m tools.builder_price --db backtest.sqlite --margin 0.15 "Arsenal v Chelsea"

Pass a COPY of the database. The VPS writes to the live file continuously.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass
from typing import Any, Optional

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1].parent / "sportybet-skill"))

from core.predictor.correlation import load_table, CorrelationTable  # noqa: E402

# Default builder margin. Research puts real builders at 15-25%; 20% is the
# midpoint. Lower it only if you have measured a real builder price against the
# fair price computed here.
DEFAULT_MARGIN = 0.20

# Market id -> (our market name, the outcome description to read).
# Only markets we can price from goal history appear here. Corners are fetched
# and shown as singles but never combined: there is no corner history yet, and
# ANALYSIS.md §8 found no repeatable corner edge from season averages.
SINGLES = [
    ("1",   "",             "Home",       "HOME"),
    ("1",   "",             "Draw",       "DRAW"),
    ("1",   "",             "Away",       "AWAY"),
    ("18",  "total=1.5",    "Over 1.5",   "O1.5"),
    ("18",  "total=2.5",    "Over 2.5",   "O2.5"),
    ("18",  "total=2.5",    "Under 2.5",  "U2.5"),
    ("18",  "total=3.5",    "Over 3.5",   "O3.5"),
    ("29",  "",             "Yes",        "BTTS"),
    ("29",  "",             "No",         "NG"),
]

# Shown for information, never combined.
CORNER_LINES = [("166", "total=8.5"), ("166", "total=9.5"), ("166", "total=10.5")]


@dataclass
class Priced:
    name: str
    probability: float
    odds: float
    margin: float


def strip_margin(outcomes: list[dict[str, Any]]) -> tuple[dict[str, float], float]:
    """
    ANALYSIS.md §3: convert a market's raw prices into margin-free probabilities.

    Scoring a model against a raw price invents roughly half the market's margin
    as edge, on every line, silently. Dividing each inverse-odds by their sum
    removes it.
    """
    inv: dict[str, float] = {}
    for o in outcomes:
        try:
            odds = float(o.get("odds") or 0)
        except (TypeError, ValueError):
            continue
        if odds > 1.0:
            inv[o.get("desc", "")] = 1.0 / odds
    total = sum(inv.values())
    if total <= 0:
        return {}, 0.0
    return {k: v / total for k, v in inv.items()}, total - 1.0


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


# On a European midweek the same tie appears twice with IDENTICAL team names —
# "Liverpool v Atletico Madrid" is both a Champions League fixture and a UEFA
# Youth League one. No amount of extra typing separates them, so the name match
# alone cannot. Prefer the senior fixture; booking the U19 game by accident is
# the most expensive silent mistake available here.
_YOUTH_MARKERS = ("youth", "u23", "u21", "u20", "u19", "u18", "reserve", "primavera", "junior")


def _is_youth(event: dict[str, Any]) -> bool:
    return any(marker in str(event).lower() for marker in _YOUTH_MARKERS)


def match_fixture(query: str, events: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """
    Find an event by a loose 'Home v Away' string.

    Deliberately strict about ambiguity: if more than one event matches, return
    nothing rather than guess. Booking the wrong fixture is the most expensive
    mistake this tool could make, and it would be invisible until settlement.
    """
    parts = re.split(r"\s+(?:v|vs|versus|-)\s+", query.strip(), maxsplit=1, flags=re.I)
    hits = []
    for ev in events:
        home, away = _norm(ev.get("homeTeamName")), _norm(ev.get("awayTeamName"))
        if len(parts) == 2:
            qh, qa = _norm(parts[0]), _norm(parts[1])
            if qh and qa and (qh in home or home in qh) and (qa in away or away in qa):
                hits.append(ev)
        else:
            q = _norm(query)
            if q and (q in home or q in away):
                hits.append(ev)
    if len(hits) == 1:
        return hits[0]

    if len(hits) > 1:
        # Drop youth/reserve clones before giving up — see _is_youth.
        senior = [h for h in hits if not _is_youth(h)]
        if len(senior) == 1:
            print(f"  · '{query}' also matched a youth fixture; using the senior one.",
                  file=sys.stderr)
            return senior[0]
        print(f"  ! '{query}' matched {len(hits)} fixtures — be more specific:", file=sys.stderr)
        for h in hits[:5]:
            print(f"      {h.get('homeTeamName')} v {h.get('awayTeamName')}"
                  f"{'  [youth]' if _is_youth(h) else ''}", file=sys.stderr)
    return None


def price_event(markets: list[dict[str, Any]]) -> tuple[dict[str, Priced], list[str]]:
    """Extract margin-free probabilities for the markets we can price."""
    priced: dict[str, Priced] = {}
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for m in markets:
        by_key.setdefault((str(m.get("id")), m.get("specifier") or ""), m.get("outcomes") or [])

    for mid, spec, desc, name in SINGLES:
        outs = by_key.get((mid, spec))
        if not outs:
            continue
        probs, margin = strip_margin(outs)
        if desc in probs:
            raw = next((float(o["odds"]) for o in outs if o.get("desc") == desc and o.get("odds")), 0.0)
            priced[name] = Priced(name, probs[desc], raw, margin)

    corners: list[str] = []
    for mid, spec in CORNER_LINES:
        outs = by_key.get((mid, spec))
        if not outs:
            continue
        probs, margin = strip_margin(outs)
        line = spec.split("=")[1]
        over = next((v for k, v in probs.items() if k.lower().startswith("over")), None)
        if over is not None:
            corners.append(f"O{line} {over*100:4.1f}% (margin {margin*100:.1f}%)")
    return priced, corners


def report(
    label: str,
    priced: dict[str, Priced],
    corners: list[str],
    table: CorrelationTable,
    margin: float,
) -> list[float]:
    """Print one fixture. Returns each combination's gap, for the honesty check."""
    print(f"\n── {label} ──")
    if not priced:
        print("   no priceable markets returned")
        return []

    print("   singles (margin-free):  " + "  ".join(
        f"{p.name}={p.probability*100:.1f}%" for p in priced.values()))
    if corners:
        print("   corners (shown, never combined — no validated history):")
        for c in corners:
            print(f"      {c}")

    names = list(priced)
    print(f"\n   {'combination':<16}{'naive':>8}{'ratio':>7}{'true':>8}{'fair':>8}{'need >':>9}  note")
    gaps: list[float] = []
    rows = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if table.is_impossible(a, b):
                rows.append((0.0, f"   {a+'+'+b:<16}{'':>8}{'':>7}{'':>8}  cannot both happen"))
                continue
            r = table.ratio(a, b)
            naive = priced[a].probability * priced[b].probability
            if r is None or naive <= 0:
                rows.append((0.0, f"   {a+'+'+b:<16}{naive*100:7.1f}%   — insufficient history"))
                continue
            true = min(naive * r, 0.999)
            fair, need = 1.0 / true, 1.0 / true / (1.0 - margin)
            gaps.append(r - 1.0)
            note = ("pull together" if r >= 1.25 else
                    "pull apart" if r <= 0.80 else "≈independent")
            rows.append((abs(r - 1.0),
                         f"   {a+'+'+b:<16}{naive*100:7.1f}%{r:7.2f}{true*100:7.1f}%{fair:8.2f}{need:9.2f}  {note}"))
    for _, line in sorted(rows, key=lambda x: -x[0]):
        print(line)
    return gaps


async def main() -> None:
    ap = argparse.ArgumentParser(description="Price bet builders against live SportyBet odds.")
    ap.add_argument("fixtures", nargs="*", help='e.g. "Arsenal v Chelsea"')
    ap.add_argument("--db", default="backtest.sqlite", help="COPY of the SportCrawl database")
    ap.add_argument("--margin", type=float, default=DEFAULT_MARGIN, help="assumed builder margin")
    ap.add_argument("--card", action="store_true", help="price the whole available card")
    ap.add_argument("--pages", type=int, default=3, help="how many pages of the card to fetch")
    args = ap.parse_args()

    if not args.fixtures and not args.card:
        ap.error("name at least one fixture, or pass --card")

    from sportybet.service import SportyBetBookerService

    table = load_table(args.db)
    if table.n == 0:
        print("No correlation table — combinations cannot be priced. Check --db.", file=sys.stderr)
        return

    svc = SportyBetBookerService()
    events = await svc.fetch_available_events(max_pages=args.pages)
    print(f"SportyBet card: {len(events)} events   ·   correlation from {table.n} matches"
          f"   ·   builder margin assumed {args.margin*100:.0f}%")

    targets = events if args.card else [e for q in args.fixtures
                                        if (e := match_fixture(q, events))]
    if not targets:
        print("\nNo fixtures matched. They may not be in the booking window yet.", file=sys.stderr)
        return

    all_gaps: list[float] = []
    for ev in targets:
        markets = await svc.fetch_event_markets(ev.get("eventId"))
        priced, corners = price_event(markets)
        label = f"{ev.get('homeTeamName')} v {ev.get('awayTeamName')}"
        all_gaps += report(label, priced, corners, table, args.margin)

    # ANALYSIS.md §2. The average is the honesty check: if the mean dependence
    # across everything examined is near zero, the top rows are noise, and a
    # ranking of noise looks exactly like a ranking of edge.
    print("\n" + "─" * 72)
    if all_gaps:
        mean = sum(all_gaps) / len(all_gaps)
        strong = sum(1 for g in all_gaps if abs(g) >= 0.25)
        print(f"{len(all_gaps)} combinations priced   ·   mean |correlation - 1| effect {mean:+.2f}"
              f"   ·   {strong} materially correlated")
        print("The 'need >' column is the decision. Read SportyBet's own builder price")
        print("in the app and compare — if it does not beat that number, do not take it.")
    print("Corners are listed but never combined: no validated history (ANALYSIS.md §8).")


if __name__ == "__main__":
    asyncio.run(main())
