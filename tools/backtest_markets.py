"""
Grade the screener's markets against played results.

Replaces the lost ``scratchpad/multiday.py``. It lives in ``tools/`` and is
committed because the number it produces — the realised hit rate per market —
is the number every ticket's expected value rests on, and a harness that only
exists on one laptop is a number nobody can re-check.

Why this exists right now
-------------------------
The 142-pick sample that produced the headline 78% breaks down as 1X 73,
Over 2.5 36, X2 15, GG 18. That is all 142. **Over 1.5 was never graded**, and
Over 1.5 is now essentially the entire Top 10/20 output. So the engine's
dominant market has no measured rate at all.

Two modes, because they cost very different things:

``--rates``
    Realised base rates straight from stored scorelines. **Zero network.**
    Answers "how often does Over 1.5 land in the population this engine picks
    from", which bounds the problem: a ticket needing 79.8% a leg is a very
    different proposition against a base rate of 84% than against 75%.

``--grade``
    Re-screens each past matchday and grades the picks the model would actually
    have made. This is the real test — a base rate says nothing about whether
    *selection* adds anything — but it costs one SofaScore history fetch per
    team, so run it on the VPS, not a laptop whose IP has been throttled for
    exactly this before.

Both read outcomes from the local match database, never from a live scrape, so
grading is reproducible and costs nothing to repeat.

Usage::

    python -m tools.backtest_markets --rates
    python -m tools.backtest_markets --rates --by-competition --min-sample 20
    python -m tools.backtest_markets --grade --from 2026-08-15 --to 2026-08-24
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

logger = logging.getLogger("SportCrawl.Backtest")

DEFAULT_DB = "ginNews.sqlite"

# Calibration bands. The 142-pick sample found the model well calibrated above
# 75% and badly calibrated at 70-75%, so the bands stay narrow up top where the
# picks actually live.
BANDS = [(0.90, 1.01), (0.85, 0.90), (0.80, 0.85), (0.75, 0.80), (0.0, 0.75)]


@dataclass
class Tally:
    """Wins and attempts for one market."""

    hits: int = 0
    total: int = 0
    prices: list[float] = field(default_factory=list)

    @property
    def rate(self) -> Optional[float]:
        return self.hits / self.total if self.total else None

    def add(self, won: bool, price: Optional[float] = None) -> None:
        self.total += 1
        self.hits += int(won)
        if price:
            self.prices.append(price)

    @property
    def break_even(self) -> Optional[float]:
        """Hit rate needed to break even at the average price taken."""
        if not self.prices:
            return None
        return 1.0 / (sum(self.prices) / len(self.prices))


# ── Outcome grading ─────────────────────────────────────────────────────
#
# One function per market, keyed by the parser token the screener emits, so a
# market can never be graded by a rule that disagrees with how it is booked.

GRADERS = {
    "Over 1.5":  lambda h, a: h + a >= 2,
    "Over 2.5":  lambda h, a: h + a >= 3,
    "Over 3.5":  lambda h, a: h + a >= 4,
    "Under 2.5": lambda h, a: h + a <= 2,
    "GG":        lambda h, a: h > 0 and a > 0,
    "NG":        lambda h, a: h == 0 or a == 0,
    "1":         lambda h, a: h > a,
    "2":         lambda h, a: a > h,
    "X":         lambda h, a: h == a,
    "1X":        lambda h, a: h >= a,
    "X2":        lambda h, a: a >= h,
    "12":        lambda h, a: h != a,
    "Draw":      lambda h, a: h == a,
}


def grade(selection: str, home_score: int, away_score: int) -> Optional[bool]:
    """Did this selection win? ``None`` if the market has no grader."""
    fn = GRADERS.get(selection)
    return None if fn is None else bool(fn(home_score, away_score))


# ── Reading played results ──────────────────────────────────────────────


def load_results(
    db_path: str = DEFAULT_DB,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    exclude_youth: bool = True,
) -> list[dict[str, Any]]:
    """Every finished match with a scoreline, as plain dicts."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    sql = [
        "select match_id, tournament_name, category_name, home_team, away_team,",
        "       home_team_id, away_team_id, start_timestamp, start_time,",
        "       home_score, away_score",
        "from football_matches",
        "where status_type = 'finished'",
        "  and home_score is not null and away_score is not null",
    ]
    params: list[Any] = []
    if date_from:
        sql.append("and date(start_time) >= ?")
        params.append(date_from)
    if date_to:
        sql.append("and date(start_time) <= ?")
        params.append(date_to)

    rows = [dict(r) for r in con.execute(" ".join(sql), params)]
    con.close()

    if exclude_youth:
        from core.predictor.leagues import is_excluded
        rows = [r for r in rows if not is_excluded(r["tournament_name"] or "")]

    return rows


# ── Mode 1: realised base rates, no network ─────────────────────────────


def report_rates(
    results: list[dict[str, Any]],
    markets: Iterable[str] = ("Over 1.5", "Over 2.5", "GG", "1", "X", "2"),
    by_competition: bool = False,
    min_sample: int = 20,
) -> str:
    """
    How often each market lands in this population.

    This is a *base rate*, not a verdict on the model. It says what a fixture
    drawn at random from the screening pool does, which is the floor any
    selection rule has to beat to be worth having.
    """
    overall: dict[str, Tally] = defaultdict(Tally)
    per_comp: dict[str, dict[str, Tally]] = defaultdict(lambda: defaultdict(Tally))

    for r in results:
        h, a = int(r["home_score"]), int(r["away_score"])
        comp = f'{r["category_name"]} / {r["tournament_name"]}'
        for m in markets:
            won = grade(m, h, a)
            if won is None:
                continue
            overall[m].add(won)
            per_comp[comp][m].add(won)

    goals = [int(r["home_score"]) + int(r["away_score"]) for r in results]
    lines = [
        "=" * 74,
        f"REALISED BASE RATES — {len(results):,} finished matches",
        f"average goals per match: {sum(goals) / len(goals):.2f}" if goals else "",
        "=" * 74,
        "",
        f"{'market':<12}{'n':>7}{'hit rate':>11}",
        "-" * 74,
    ]
    for m in markets:
        t = overall.get(m)
        if t and t.total:
            lines.append(f"{m:<12}{t.total:>7,}{t.rate:>10.1%}")

    if by_competition:
        lines += ["", "By competition (sample >= %d):" % min_sample, "-" * 74,
                  f"{'competition':<44}{'n':>6}" + "".join(f"{m:>10}" for m in markets)]
        ranked = sorted(per_comp.items(), key=lambda kv: -sum(t.total for t in kv[1].values()))
        for comp, tallies in ranked:
            n = max(t.total for t in tallies.values())
            if n < min_sample:
                continue
            row = f"{comp[:43]:<44}{n:>6}"
            for m in markets:
                t = tallies.get(m)
                row += f"{t.rate:>9.0%} " if t and t.total else f"{'—':>10}"
            lines.append(row)

    lines += [
        "",
        "A base rate is a floor, not a result. It says what a random fixture from",
        "this pool does. Whether the screener's SELECTION beats it is what --grade",
        "measures, and only that answers whether a ticket is worth its price.",
    ]
    return "\n".join(l for l in lines if l is not None)


# ── Mode 2: grade the model's actual picks ──────────────────────────────


async def grade_picks(
    results: list[dict[str, Any]],
    form_matches: int = 10,
    top_n: int = 50,
    sample_per_day: int = 40,
    seed: int = 7,
) -> str:
    """
    Re-screen each matchday and grade the picks the model would have made.

    Form is fetched with ``before_ts`` set to kickoff, so a fixture is never
    predicted using results from after it was played. Getting this wrong is the
    single easiest way to manufacture a fake hit rate, and it has happened in
    this repo before.

    ``sample_per_day`` caps how many fixtures each matchday contributes, because
    the cost here is one SofaScore history fetch per *team*. A full day of ~340
    fixtures is ~680 fetches, and ten of those is the request volume that got an
    IP blocked before. Sampling is sound for the calibration question — each
    pick is graded against its own predicted probability, so a smaller candidate
    pool does not bias whether an 85% pick hits 85%. It is NOT sound for
    simulating a real Top 10, which selects the best of a full card; raise the
    cap for that, and expect it to cost accordingly.
    """
    import random

    rng = random.Random(seed)
    from core.predictor.enrich import fetch_team_forms
    from core.predictor.filter import Fixture
    from core.predictor.screen import screen_fixtures

    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in results:
        by_day[str(r["start_time"])[:10]].append(r)

    market_tally: dict[str, Tally] = defaultdict(Tally)
    band_tally: dict[tuple[float, float], Tally] = defaultdict(Tally)
    graded = 0

    for day in sorted(by_day):
        day_rows = by_day[day]
        fixtures = [
            Fixture(
                match_id=int(r["match_id"]),
                tournament=r["tournament_name"] or "",
                category=r["category_name"] or "",
                home_name=r["home_team"],
                away_name=r["away_team"],
                home_id=int(r["home_team_id"] or 0),
                away_id=int(r["away_team_id"] or 0),
                start_utc=str(r["start_time"]),
                start_local=str(r["start_time"]),
            )
            for r in day_rows
            if r["home_team_id"] and r["away_team_id"]
        ]
        if not fixtures:
            continue

        # Deterministic sample, so a re-run reproduces the same number.
        if sample_per_day and len(fixtures) > sample_per_day:
            keep = set(rng.sample(range(len(fixtures)), sample_per_day))
            fixtures = [f for i, f in enumerate(fixtures) if i in keep]

        # Cut form at the earliest kickoff of the day, so no fixture on the
        # card can see any other fixture's result either.
        stamps = [int(r["start_timestamp"]) for r in day_rows if r["start_timestamp"]]
        before_ts = min(stamps) if stamps else None

        logger.info("Matchday %s: %d fixtures (~%d SofaScore fetches)",
                    day, len(fixtures), len(fixtures) * 2)
        forms = await fetch_team_forms(
            fixtures, form_matches=form_matches, before_ts=before_ts
        )
        picks = screen_fixtures(fixtures=fixtures, forms=forms, limit=top_n, max_per_fixture=1)

        scores = {int(r["match_id"]): (int(r["home_score"]), int(r["away_score"]))
                  for r in day_rows}
        for pick in picks:
            actual = scores.get(pick.fixture.match_id)
            if not actual:
                continue
            won = grade(pick.selection, *actual)
            if won is None:
                logger.warning("No grader for selection %r — skipped.", pick.selection)
                continue
            market_tally[pick.selection].add(won)
            for band in BANDS:
                if band[0] <= pick.probability < band[1]:
                    band_tally[band].add(won)
                    break
            graded += 1

    lines = [
        "=" * 74,
        f"GRADED MODEL PICKS — {graded} picks over {len(by_day)} matchdays",
        "=" * 74,
        "",
        f"{'market':<12}{'n':>6}{'hit rate':>11}{'  vs base rate':>16}",
        "-" * 74,
    ]

    base = {m: Tally() for m in market_tally}
    for r in results:
        h, a = int(r["home_score"]), int(r["away_score"])
        for m in base:
            won = grade(m, h, a)
            if won is not None:
                base[m].add(won)

    for m, t in sorted(market_tally.items(), key=lambda kv: -kv[1].total):
        b = base[m].rate
        delta = (t.rate - b) if (t.rate is not None and b is not None) else None
        edge = f"{b:>7.1%} ({delta:+.1f}pp)" if delta is not None else "—"
        lines.append(f"{m:<12}{t.total:>6}{t.rate:>10.1%}{edge:>18}")

    lines += ["", "Calibration by predicted band:", "-" * 74,
              f"{'band':<14}{'n':>6}{'predicted':>12}{'actual':>10}"]
    for band in BANDS:
        t = band_tally.get(band)
        if not t or not t.total:
            continue
        mid = (band[0] + min(band[1], 1.0)) / 2
        lines.append(f"{band[0]:.0%}-{min(band[1],1.0):.0%}{'':<6}{t.total:>6}{mid:>11.0%}{t.rate:>10.1%}")

    lines += [
        "",
        "'vs base rate' is the number that matters: a market that hits 84% when",
        "the population also hits 84% has a selection rule adding nothing, however",
        "good the raw hit rate looks.",
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--from", dest="date_from", help="YYYY-MM-DD")
    ap.add_argument("--to", dest="date_to", help="YYYY-MM-DD")
    ap.add_argument("--rates", action="store_true",
                    help="realised base rates from stored scores (no network)")
    ap.add_argument("--grade", action="store_true",
                    help="re-screen and grade the model's picks (fetches SofaScore)")
    ap.add_argument("--by-competition", action="store_true")
    ap.add_argument("--min-sample", type=int, default=20)
    ap.add_argument("--form-matches", type=int, default=10)
    ap.add_argument("--top", type=int, default=50)
    ap.add_argument("--sample-per-day", type=int, default=40,
                    help="fixtures sampled per matchday (0 = all; costs ~2 "
                         "SofaScore fetches per fixture, so 0 on a full card "
                         "is thousands of requests)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s │ %(message)s")

    results = load_results(args.db, args.date_from, args.date_to)
    if not results:
        print("No finished matches with scorelines in that range.")
        return
    if not args.rates and not args.grade:
        args.rates = True

    if args.rates:
        print(report_rates(results, by_competition=args.by_competition,
                           min_sample=args.min_sample))
    if args.grade:
        print()
        days = len({str(r["start_time"])[:10] for r in results})
        per_day = args.sample_per_day or (len(results) // max(days, 1))
        est = days * per_day * 2
        print(f"About to fetch ~{est:,} SofaScore team histories "
              f"({days} matchdays x ~{per_day} fixtures x 2 teams).")
        if est > 2000:
            print("That is a lot. Narrow with --from/--to or lower "
                  "--sample-per-day if this IP matters.")
        print()
        print(asyncio.run(grade_picks(results, form_matches=args.form_matches,
                                      top_n=args.top,
                                      sample_per_day=args.sample_per_day)))


if __name__ == "__main__":
    main()
