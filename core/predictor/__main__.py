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

from core.predictor import draws as draws_module
from core.predictor.draw_ledger import record_picks, summarise
from core.predictor.draws import DEFAULT_RHO, DrawScreenStats, screen_draws
from core.predictor.enrich import fetch_team_forms
from core.predictor.filter import filter_fixtures, load_fixture_file
from core.predictor.format import format_picks, format_priced, format_report
from core.predictor.odds import attach_odds, filter_by_edge
from core.predictor.screen import screen_fixtures
from core.predictor.tickets import build_ladder, format_ladder


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
    draws = parser.add_argument_group(
        "draw track",
        "A parallel screen for draws, using a Dixon-Coles corrected goals model. "
        "Shares nothing with the main screener's floors, calibration or results.",
    )
    draws.add_argument(
        "--draws",
        action="store_true",
        help="Screen for draws instead of the standard markets, and build a ticket ladder",
    )
    draws.add_argument(
        "--ticket-shape",
        default="5,5",
        help="Legs per accumulator, comma separated (default: 5,5 — two five-folds off ten picks)",
    )
    draws.add_argument(
        "--rho",
        type=float,
        default=None,
        help="Dixon-Coles dependence parameter (default: -0.13, unfitted — see tools/fit_rho.py)",
    )
    draws.add_argument(
        "--draw-floor",
        type=float,
        default=None,
        help="Minimum draw probability (default: 0.28). Lower it if the card comes back thin.",
    )
    draws.add_argument(
        "--draw-conviction-floor",
        type=float,
        default=None,
        help="Minimum draw conviction (default: 0.24)",
    )
    draws.add_argument(
        "--max-payout",
        type=float,
        default=None,
        help="SportyBet's per-ticket payout cap, to show where stake stops earning",
    )
    draws.add_argument(
        "--overlapping",
        action="store_true",
        help="Let tickets share legs (default: disjoint, so one bad leg cannot kill every ticket)",
    )
    draws.add_argument(
        "--no-ledger",
        action="store_true",
        help="Do not append these picks to storage/draw_ledger.jsonl",
    )

    parser.add_argument("--show-dropped", action="store_true", help="Show a sample of filtered-out fixtures")
    parser.add_argument(
        "--allow-unlisted",
        action="store_true",
        help="Bypass the competition allowlist (exploration only — not for real picks)",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    return parser


async def run_draws(args, fixtures, forms, stats) -> int:
    """
    The draw track: screen, price, and spend the picks as a ticket ladder.

    Prices are fetched unconditionally here, unlike the main path where they
    are opt-in. A draw pick without a price cannot be judged at all — at a ~30%
    hit rate the price is the entire question, and an unpriced leg makes the
    accumulator's odds unknowable rather than merely uncertain.
    """
    # The floors are module constants so screen_draw and the diagnostic read the
    # same values; overriding them here keeps the first live runs tunable
    # without an edit-rerun cycle.
    if args.draw_floor is not None:
        draws_module.DRAW_PROBABILITY_FLOOR = args.draw_floor
    if args.draw_conviction_floor is not None:
        draws_module.DRAW_CONVICTION_FLOOR = args.draw_conviction_floor

    rho = args.rho if args.rho is not None else DEFAULT_RHO
    stats_draws = DrawScreenStats()
    picks = screen_draws(fixtures, forms, limit=args.top or 10, rho=rho, stats=stats_draws)

    print(stats_draws.summary())
    print()

    if not picks:
        print("No fixture cleared the draw floors.")
        print("Normal on a card with no evenly-matched low-scoring games — not an error.")
        print("If it keeps happening, the counts above say which floor to move.")
        return 0

    if len(picks) < sum(int(p) for p in args.ticket_shape.split(",") if p.strip()):
        print(
            f"Only {len(picks)} candidates for a {args.ticket_shape} ladder — "
            "some tickets will be skipped rather than shortened."
        )
        print()

    priced = await attach_odds(picks)
    if args.min_edge is not None:
        priced = filter_by_edge(priced, min_edge=args.min_edge)

    if not args.no_ledger:
        record_picks(priced)

    try:
        shape = [int(part) for part in args.ticket_shape.split(",") if part.strip()]
    except ValueError:
        print(f"Could not read --ticket-shape {args.ticket_shape!r}; expected e.g. 5,5", file=sys.stderr)
        return 1

    tickets = build_ladder(priced, shape=shape, disjoint=not args.overlapping)

    if args.picks_only:
        for ticket in tickets:
            print("\n".join(ticket.lines))
            print()
        return 0

    print(format_report([p.pick for p in priced], stats, show_dropped=args.show_dropped))
    print()
    print(format_priced(priced))
    print()
    print(format_ladder(tickets, max_payout=args.max_payout))
    print()
    print(summarise())
    return 0


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

    if args.draws:
        return await run_draws(args, fixtures, forms, stats)

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
