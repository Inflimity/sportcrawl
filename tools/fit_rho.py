"""
Fit the Dixon-Coles dependence parameter by maximum likelihood.

``core.predictor.draws.DEFAULT_RHO`` is -0.13, a conventional value from the
literature rather than one measured on the competitions this engine actually
bets. That matters more than a default usually would: the entire claimed draw
edge is three to four percentage points, and rho is the parameter that produces
it. A rho fitted at -0.05 would roughly halve the edge; one at -0.20 would
inflate it. Neither is knowable without fitting.

Usage
-----
::

    python -m tools.fit_rho matches.jsonl
    python -m tools.fit_rho --self-test

Input is one JSON object per line, each a historical match with the model's
expected goals and the scoreline that actually happened::

    {"lam_home": 1.31, "lam_away": 1.08, "home_goals": 1, "away_goals": 1}

Producing that file means running ``expected_goals()`` over past fixtures with
``before_ts`` set, so form is cut off at kickoff. Without ``before_ts`` the
lambdas silently include results from after the match, and the fitted rho is
worthless — the same trap the existing backtest harness documents.

Several hundred matches gives a usable point estimate; a thousand or more
tightens the interval enough to trust the third decimal.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path
from typing import Sequence

from core.predictor.draws import RHO_MAX, RHO_MIN, dc_tau
from core.predictor.screen import _poisson_pmf

Match = tuple[float, float, int, int]


def log_likelihood(matches: Sequence[Match], rho: float) -> float:
    """Total log-likelihood of the observed scorelines under this rho."""
    total = 0.0
    for lam_home, lam_away, home_goals, away_goals in matches:
        p = (
            _poisson_pmf(home_goals, lam_home)
            * _poisson_pmf(away_goals, lam_away)
            * dc_tau(home_goals, away_goals, lam_home, lam_away, rho)
        )
        if p <= 0:
            return -math.inf
        total += math.log(p)
    return total


def fit(matches: Sequence[Match], steps: int = 400) -> tuple[float, float]:
    """
    Grid-search rho over its valid range, then refine around the best cell.

    A grid rather than a solver because the search is one-dimensional and
    cheap, and because seeing the whole curve is what tells you whether the
    maximum is a real peak or a flat region the data cannot distinguish.
    """
    if not matches:
        raise ValueError("no matches to fit")

    # tau must stay positive: rho > -1/max(lam) across every match.
    largest_lam = max(max(m[0], m[1]) for m in matches)
    lower = max(RHO_MIN, -1.0 / largest_lam + 1e-6)
    upper = RHO_MAX

    best_rho, best_ll = lower, -math.inf
    for i in range(steps + 1):
        rho = lower + (upper - lower) * i / steps
        ll = log_likelihood(matches, rho)
        if ll > best_ll:
            best_rho, best_ll = rho, ll

    # Refine in the neighbouring cells.
    span = (upper - lower) / steps
    lower, upper = best_rho - span, best_rho + span
    for i in range(steps + 1):
        rho = lower + (upper - lower) * i / steps
        ll = log_likelihood(matches, rho)
        if ll > best_ll:
            best_rho, best_ll = rho, ll

    return best_rho, best_ll


def load_matches(path: Path) -> list[Match]:
    matches: list[Match] = []
    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                matches.append(
                    (
                        float(row["lam_home"]),
                        float(row["lam_away"]),
                        int(row["home_goals"]),
                        int(row["away_goals"]),
                    )
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                print(f"skipping line {number}: {exc}", file=sys.stderr)
    return matches


def synthesise(count: int, true_rho: float, seed: int = 7) -> list[Match]:
    """Draw matches from a known rho, so the fitter can be checked against it."""
    rng = random.Random(seed)
    matches: list[Match] = []

    for _ in range(count):
        lam_home = rng.uniform(0.8, 2.2)
        lam_away = rng.uniform(0.6, 1.9)

        cells, weights = [], []
        for h in range(9):
            for a in range(9):
                cells.append((h, a))
                weights.append(
                    _poisson_pmf(h, lam_home)
                    * _poisson_pmf(a, lam_away)
                    * dc_tau(h, a, lam_home, lam_away, true_rho)
                )
        home_goals, away_goals = rng.choices(cells, weights=weights, k=1)[0]
        matches.append((lam_home, lam_away, home_goals, away_goals))

    return matches


def report(matches: Sequence[Match]) -> None:
    rho, ll = fit(matches)
    draws = sum(1 for m in matches if m[2] == m[3])

    print(f"matches:        {len(matches)}")
    print(f"observed draws: {draws} ({draws / len(matches):.2%})")
    print(f"fitted rho:     {rho:+.4f}")
    print(f"log-likelihood: {ll:,.2f}")
    print()
    print("log-likelihood by rho:")
    for probe in (-0.20, -0.15, -0.13, -0.10, -0.05, 0.0):
        if probe > -1.0 / max(max(m[0], m[1]) for m in matches):
            delta = log_likelihood(matches, probe) - ll
            marker = "  <- current default" if abs(probe + 0.13) < 1e-9 else ""
            print(f"  rho {probe:+.2f}   {delta:+9.2f} vs best{marker}")
    print()
    if abs(rho + 0.13) > 0.04:
        print(
            f"Fitted rho is {abs(rho + 0.13):.3f} away from the -0.13 default. "
            "Set DEFAULT_RHO in core/predictor/draws.py and re-read the draw "
            "probabilities before trusting any of them."
        )
    else:
        print("Fitted rho is close to the default; the draw probabilities stand as computed.")


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.fit_rho", description=__doc__)
    parser.add_argument("matches", nargs="?", help="JSONL of graded historical matches")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Fit against synthetic data drawn from a known rho, to check the fitter itself",
    )
    args = parser.parse_args()

    if args.self_test:
        for true_rho in (-0.18, -0.13, -0.05):
            matches = synthesise(20000, true_rho)
            fitted, _ = fit(matches)
            status = "ok" if abs(fitted - true_rho) < 0.02 else "OFF"
            print(f"true rho {true_rho:+.3f}  ->  fitted {fitted:+.3f}   [{status}]")
        return 0

    if not args.matches:
        parser.error("give a matches file, or --self-test")

    path = Path(args.matches)
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 1

    matches = load_matches(path)
    if not matches:
        print("no usable matches in file", file=sys.stderr)
        return 1

    report(matches)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
