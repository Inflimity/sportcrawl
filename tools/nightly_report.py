"""
Print (or send) the nightly ticket result report for a given date.

The scheduler in main.py runs this automatically at 23:50 WAT. This is the
manual door: re-run a night whose late fixtures had not finished, check a past
date, or test the formatting without waiting for the clock.

    python3 -m tools.nightly_report                  # today, printed
    python3 -m tools.nightly_report --date 2026-09-18
    python3 -m tools.nightly_report --send           # also push to Telegram
    python3 -m tools.nightly_report --days 7         # last week, one per day

Re-running is cheap and safe: settled scores are cached, so a second run of the
same night fetches only the fixtures that have finished since the first.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.nightly_report import build_nightly_report  # noqa: E402


def to_plain(html_text: str) -> str:
    """Strip the Telegram tags so the report reads in a terminal."""
    text = re.sub(r"<code>(.*?)</code>", r"\1", html_text)
    text = re.sub(r"</?(b|i|u|s|code|pre)>", "", text)
    return text


async def run(args: argparse.Namespace) -> None:
    from config.settings import get_settings

    settings = get_settings()
    tz = settings.app_timezone
    today = datetime.now(ZoneInfo(tz)).date()

    dates = [
        (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(args.days)
    ]
    if args.date:
        dates = [args.date]

    notifier = None
    if args.send:
        from notifiers.telegram_bot import TelegramNotifier

        notifier = TelegramNotifier(settings)

    for date_str in dates:
        message, tickets = await build_nightly_report(
            date_str=date_str,
            tz=tz,
            log_path=args.log,
            cache_path=args.cache,
            show_legs=not args.summary,
        )
        print(to_plain(message))
        print()

        if notifier and tickets:
            try:
                await notifier.send_custom_message(text=message, parse_mode="HTML")
                print(f"→ sent {date_str} to Telegram")
            except Exception as e:  # noqa: BLE001
                print(f"→ Telegram send failed for {date_str}: {e}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--date", help="a single local date, YYYY-MM-DD")
    ap.add_argument("--days", type=int, default=1, help="how many days back to report")
    ap.add_argument("--log", default="logs/tickets.jsonl")
    ap.add_argument("--cache", default="logs/outcomes.json")
    ap.add_argument("--send", action="store_true", help="also push to Telegram")
    ap.add_argument("--summary", action="store_true", help="hide the per-leg detail")
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
