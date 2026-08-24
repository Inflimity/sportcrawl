"""
CLI for SportCrawl's prediction engine.

    python -m core.predictor ~/Downloads/sportcrawl_upcoming_2026-08-23.json
    python -m core.predictor fixtures.json --top 10 --picks-only
    python -m core.predictor fixtures.json --allow-unlisted --show-dropped

``--picks-only`` prints nothing but the selection lines, so the output can be
piped straight into the booker::

    python -m core.predictor fixtures.json --picks-only > picks.txt
    python main.py --book "$(cat picks.txt)"
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from core.predictor.enrich import fetch_team_forms
from core.predictor.filter import filter_fixtures, load_fixture_file
from core.predictor.format import format_picks, format_priced, format_report
from core.predictor.odds import attach_odds, filter_by_edge
from core.predictor.screen import screen_fixtures


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m core.predictor",
        description="Screen a SofaScore fixture dump into high-conviction betting selections.",
    )
    parser.add_argument("fixtures", help="Path to a sportcrawl_upcoming_*.json file")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only enrich and screen the first N tradeable fixtures (fast smoke test)",
    )
    parser.add_argument("--top", type=int, default=None, help="Keep only the N highest-conviction picks")
    parser.add_argument(
        "--max-per-fixture",
        type=int,
        default=1,
        help="Selections per match (default: 1 — picks on one match are correlated)",
    )
    parser.add_argument("--form-matches", type=int, default=10, help="Recent matches per team (default: 10)")
    parser.add_argument("--picks-only", action="store_true", help="Print only booker-ready selection lines")
    parser.add_argument(
        "--odds",
        action="store_true",
        help="Fetch live SportyBet prices and show the edge against the model",
    )
    parser.add_argument(
        "--min-edge",
        type=float,
        default=None,
        help="With --odds, keep only picks whose edge over the implied price clears this (e.g. 0.05)",
    )
    parser.add_argument("--show-dropped", action="store_true", help="Show a sample of filtered-out fixtures")
    parser.add_argument(
        "--allow-unlisted",
        action="store_true",
        help="Bypass the competition allowlist (exploration only — not for real picks)",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    return parser


async def run(args: argparse.Namespace) -> int:
    raw = load_fixture_file(args.fixtures)
    if not raw:
        print(f"No matches found in {args.fixtures}", file=sys.stderr)
        return 1

    fixtures, stats = filter_fixtures(raw, allow_unlisted=args.allow_unlisted)
    if args.limit:
        fixtures = fixtures[: args.limit]
    if not fixtures:
        if not args.picks_only:
            print(format_report([], stats, show_dropped=args.show_dropped))
        return 0

    forms = await fetch_team_forms(fixtures, form_matches=args.form_matches)
    picks = screen_fixtures(
        fixtures,
        forms,
        limit=args.top,
        max_per_fixture=args.max_per_fixture,
    )

    if args.odds or args.min_edge is not None:
        priced = await attach_odds(picks)
        if args.min_edge is not None:
            priced = filter_by_edge(priced, min_edge=args.min_edge)
            picks = [p.pick for p in priced]
        if args.picks_only:
            print(format_picks(picks))
        else:
            print(format_report(picks, stats, show_dropped=args.show_dropped))
            print()
            print(format_priced(priced))
        return 0

    if args.picks_only:
        print(format_picks(picks))
    else:
        print(format_report(picks, stats, show_dropped=args.show_dropped))
    return 0


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
