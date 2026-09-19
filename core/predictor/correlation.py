"""
Empirical correlation between markets in the same match.

WHY THIS EXISTS
---------------
A bet builder combines several markets from ONE match. The legs are not
independent, so multiplying their probabilities is wrong — and wrong in both
directions, which is what makes it dangerous:

    HOME + HT_HOME    naive 16.1%   actual 29.2%    ratio 1.81
    U2.5 + BTTS       naive 20.5%   actual  7.5%    ratio 0.37

Multiply, and you understate the first by nearly half and overstate the second
by nearly triple. A bookmaker prices these with a copula model. We can do better
than a model for the markets we have history on: measure how often the outcomes
actually co-occur.

Everything here is derived from FT and HT scores, which the database already
stores for every finished match. No new data source, no new request. Corners are
deliberately absent — see match_stats.py; there is no corner history yet, and a
correlation computed from a handful of matches would be exactly the "maximum
over noise" that ANALYSIS.md §2 warns about.

WHAT THIS DOES NOT DO
---------------------
It does not tell you a builder is +EV. It tells you what the combination is
really worth, so that the price you are offered can be compared against
something true. ANALYSIS.md §7 still applies: strip the margin from both sides
before comparing, and a builder's margin is 15-25%, not the 2-6% of a single.
"""

from __future__ import annotations

import itertools
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

logger = logging.getLogger("SportCrawl.Predictor.Correlation")

# Below this many co-occurrences, a joint rate is noise. ANALYSIS.md §2 measured
# per-line noise at 7.4% sd; a joint cell seen 30 times has a standard error
# around 9 points, which is wider than any edge worth chasing.
MIN_JOINT_SAMPLE = 30


# --------------------------------------------------------------------------
# Market definitions — every outcome derivable from FT + HT scores
# --------------------------------------------------------------------------
# Each entry maps a selection name to a predicate over (home, away, ht_home,
# ht_away). Adding a market here makes it available to the whole module: the
# single rates, the pairwise table, and the builder sheet.
MARKETS: dict[str, Callable[[int, int, int, int], bool]] = {
    # Full-time result
    "HOME":         lambda h, a, hh, ah: h > a,
    "DRAW":         lambda h, a, hh, ah: h == a,
    "AWAY":         lambda h, a, hh, ah: a > h,
    "HOME_OR_DRAW": lambda h, a, hh, ah: h >= a,
    "AWAY_OR_DRAW": lambda h, a, hh, ah: a >= h,
    # Half-time result
    "HT_HOME":      lambda h, a, hh, ah: hh > ah,
    "HT_DRAW":      lambda h, a, hh, ah: hh == ah,
    "HT_AWAY":      lambda h, a, hh, ah: ah > hh,
    # Goals, full match
    "O0.5":         lambda h, a, hh, ah: h + a >= 1,
    "O1.5":         lambda h, a, hh, ah: h + a >= 2,
    "O2.5":         lambda h, a, hh, ah: h + a >= 3,
    "O3.5":         lambda h, a, hh, ah: h + a >= 4,
    "U1.5":         lambda h, a, hh, ah: h + a < 2,
    "U2.5":         lambda h, a, hh, ah: h + a < 3,
    "U3.5":         lambda h, a, hh, ah: h + a < 4,
    # Goals by half
    "HT_O0.5":      lambda h, a, hh, ah: hh + ah >= 1,
    "HT_O1.5":      lambda h, a, hh, ah: hh + ah >= 2,
    "2H_O0.5":      lambda h, a, hh, ah: (h - hh) + (a - ah) >= 1,
    "2H_O1.5":      lambda h, a, hh, ah: (h - hh) + (a - ah) >= 2,
    # Scoring
    "BTTS":         lambda h, a, hh, ah: h > 0 and a > 0,
    "NG":           lambda h, a, hh, ah: not (h > 0 and a > 0),
    "HOME_SCORES":  lambda h, a, hh, ah: h > 0,
    "AWAY_SCORES":  lambda h, a, hh, ah: a > 0,
    "HOME_CS":      lambda h, a, hh, ah: a == 0,   # clean sheet
    "AWAY_CS":      lambda h, a, hh, ah: h == 0,
    # Combination shapes people actually book
    "HOME_AND_O1.5": lambda h, a, hh, ah: h > a and h + a >= 2,
    "HOME_WIN_TO_NIL": lambda h, a, hh, ah: h > a and a == 0,
}

# Pairs that cannot both be true. A bookmaker's builder rejects these outright;
# listing them means we can say "impossible" rather than reporting 0.0% and
# leaving the reader to wonder whether it is merely rare.
def _impossible(x: str, y: str, rates: dict[str, float], joint: dict[tuple[str, str], int]) -> bool:
    return joint.get(_key(x, y), 0) == 0 and rates.get(x, 0) > 0.02 and rates.get(y, 0) > 0.02


def _key(x: str, y: str) -> tuple[str, str]:
    """Pair keys are order-independent, so store them sorted."""
    return (x, y) if x <= y else (y, x)


@dataclass
class CorrelationTable:
    """
    Empirical single and joint rates over a set of finished matches.

    ``n`` is the sample size. ``single[m]`` is P(m). ``joint[(x, y)]`` is the
    COUNT of matches where both happened — a count rather than a rate so that
    callers can apply their own sample-size test.
    """

    n: int = 0
    single: dict[str, float] = field(default_factory=dict)
    joint: dict[tuple[str, str], int] = field(default_factory=dict)

    def p(self, market: str) -> Optional[float]:
        return self.single.get(market)

    def joint_p(self, x: str, y: str) -> Optional[float]:
        """P(x AND y), or None when the pair is too rare to state."""
        if self.n == 0:
            return None
        c = self.joint.get(_key(x, y))
        if c is None:
            return None
        if c < MIN_JOINT_SAMPLE:
            return None
        return c / self.n

    def ratio(self, x: str, y: str) -> Optional[float]:
        """
        P(x AND y) / (P(x) * P(y)).

        Above 1: the outcomes pull together, and multiplying UNDERSTATES the
        chance — the true price is shorter than a naive punter expects, and the
        book will already have shortened it.

        Below 1: they pull apart, and multiplying OVERSTATES the chance — the
        long odds on offer are long for a reason.
        """
        pj = self.joint_p(x, y)
        px, py = self.single.get(x), self.single.get(y)
        if pj is None or not px or not py:
            return None
        return pj / (px * py)

    def is_impossible(self, x: str, y: str) -> bool:
        """True when the pair never once co-occurred despite both being common."""
        return _impossible(x, y, self.single, self.joint)


def build_table(rows: Iterable[tuple[int, int, int, int]]) -> CorrelationTable:
    """
    Build the table from (home, away, ht_home, ht_away) tuples.

    Pure, so it can be tested and so a backtest can pass a filtered slice
    (one league, one window) without touching the database.
    """
    names = list(MARKETS)
    single_counts = {k: 0 for k in names}
    joint_counts: dict[tuple[str, str], int] = {}
    n = 0

    for h, a, hh, ah in rows:
        if h is None or a is None or hh is None or ah is None:
            continue
        # A half-time score above the full-time score means one of the two is
        # wrong. Dropping the match is safer than trusting either number.
        if hh > h or ah > a:
            continue
        n += 1
        hit = [k for k in names if MARKETS[k](h, a, hh, ah)]
        for k in hit:
            single_counts[k] += 1
        for x, y in itertools.combinations(hit, 2):
            key = _key(x, y)
            joint_counts[key] = joint_counts.get(key, 0) + 1

    if n == 0:
        return CorrelationTable()

    return CorrelationTable(
        n=n,
        single={k: c / n for k, c in single_counts.items()},
        joint=joint_counts,
    )


def load_table(
    db_path: str,
    league: Optional[str] = None,
    min_matches: int = 200,
) -> CorrelationTable:
    """
    Build the table from a SportCrawl database.

    Pass a copy of the database, not the live file — the VPS writes to it
    continuously and a long read will contend with that.

    ``league`` restricts to one tournament. It usually should not be used yet:
    a single competition rarely has enough finished matches here to clear
    MIN_JOINT_SAMPLE on the rarer pairs, and a table built from 60 matches will
    report correlations that are entirely noise. The function refuses below
    ``min_matches`` rather than returning a confident-looking table.
    """
    sql = """
        SELECT home_score, away_score, home_score_ht, away_score_ht
        FROM football_matches
        WHERE status_type = 'finished'
          AND home_score IS NOT NULL AND away_score IS NOT NULL
          AND home_score_ht IS NOT NULL AND away_score_ht IS NOT NULL
    """
    params: list[str] = []
    if league:
        sql += " AND tournament_name = ?"
        params.append(league)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    table = build_table(rows)
    if table.n < min_matches:
        logger.warning(
            "Correlation table has only %d matches%s — below the %d minimum. "
            "Rates from this sample are noise; returning it empty.",
            table.n,
            f" for {league}" if league else "",
            min_matches,
        )
        return CorrelationTable()

    logger.info("Correlation table built from %d matches%s", table.n, f" ({league})" if league else "")
    return table
