"""
Capture the closing price for every draw pick shortly before kickoff.

Why this is the most valuable thing running
-------------------------------------------
Hit rate needs 400-500 graded picks before it separates a real edge from noise
— six or seven weeks at ten a day. Closing line value reads at around 150,
which is a fortnight. Beating the closing line is also the more honest signal:
it says the market moved toward your price, which is evidence about the model
rather than about variance.

That signal only exists if it is captured live. SportyBet publishes no
historical odds, so a price not taken before kickoff is gone permanently. Every
day this does not run is a day of picks that can never be evaluated this way.

Cost
----
SportyBet only — this never touches SofaScore, so it cannot contribute to the
rate limiting that blocked the local IP. Each pick costs exactly one
``factsCenter/event`` call because ``event_id`` was stored when the pick was
recorded; without it, each capture would need a paginated sweep of ~1029
events to re-find a fixture already resolved that morning.

Usage
-----
::

    python -m tools.closing_sweep                 # one pass, then exit
    python -m tools.closing_sweep --watch         # loop, for a service or tmux
    python -m tools.closing_sweep --window 600    # capture inside 10 minutes

One pass suits a cron entry every ten minutes. ``--watch`` suits running
alongside the bot. Either way it is idempotent: a row with a stored closing
price is never re-fetched.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone

from core.market_mapper import resolve_market_selection
from core.prediction_parser import parse_prediction_line
from core.predictor.draw_ledger import (
    apply_closing_odds,
    pending_closing_capture,
    row_key,
)
from services.sportybet_service import SportyBetBookerService

logger = logging.getLogger("SportCrawl.ClosingSweep")


async def capture_once(
    service: SportyBetBookerService,
    window_seconds: int = 900,
) -> int:
    """
    Price every pick kicking off inside the window. Returns rows updated.

    Markets are cached per event: several picks can share a fixture, and a
    repeat call would spend a request to receive the identical payload.
    """
    pending = pending_closing_capture(within_seconds=window_seconds)
    if not pending:
        return 0

    logger.info("Capturing closing prices for %d pick(s)", len(pending))

    markets_cache: dict[str, list] = {}
    captured: dict[tuple[str, object], float] = {}

    for row in pending:
        event_id = row["event_id"]
        line = row.get("line") or ""

        if event_id not in markets_cache:
            markets_cache[event_id] = await service.fetch_event_markets(event_id)
        markets = markets_cache[event_id]

        if not markets:
            logger.warning("No markets returned for %s (%s) — likely suspended", line, event_id)
            continue

        bet = parse_prediction_line(line)
        if not bet:
            logger.warning("Ledger line no longer parses, skipping: %r", line)
            continue

        # Resolved through the same mapper that priced it originally, so the
        # opening and closing figures describe the same outcome.
        resolved = resolve_market_selection(bet, markets)
        if not resolved:
            logger.warning("Draw market not active for %s — no closing price", line)
            continue

        try:
            odds = float(resolved.get("odds") or 0) or None
        except (TypeError, ValueError):
            odds = None

        if not odds:
            logger.warning("Draw market for %s carried no price", line)
            continue

        captured[row_key(row)] = odds
        opening = row.get("odds")
        if opening:
            drift = (opening - odds) / opening
            logger.info(
                "%s: %.2f -> %.2f (%+.1f%%) %s",
                line,
                opening,
                odds,
                drift * 100,
                "shortened, market agrees" if odds < opening else "drifted",
            )

    return apply_closing_odds(captured)


async def run(args: argparse.Namespace) -> int:
    service = SportyBetBookerService(country_code=args.country)

    if not args.watch:
        updated = await capture_once(service, window_seconds=args.window)
        print(f"Captured {updated} closing price(s).")
        return 0

    logger.info(
        "Watching for kickoffs inside %ds, polling every %ds. Ctrl-C to stop.",
        args.window,
        args.interval,
    )
    try:
        while True:
            try:
                updated = await capture_once(service, window_seconds=args.window)
                if updated:
                    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    print(f"[{stamp}] captured {updated} closing price(s)")
            except Exception as exc:
                # A sweep that dies on one bad response stops capturing for the
                # rest of the day, and those prices cannot be recovered later.
                logger.warning("Sweep pass failed, continuing: %s", exc)
            await asyncio.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.closing_sweep",
        description="Capture closing draw prices into the draw ledger.",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=900,
        help="Seconds before kickoff to capture within (default: 900)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="With --watch, seconds between passes (default: 300)",
    )
    parser.add_argument("--watch", action="store_true", help="Run continuously")
    parser.add_argument("--country", default="ng", help="SportyBet country code (default: ng)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
