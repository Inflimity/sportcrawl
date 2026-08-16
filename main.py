"""
SportCrawl — SofaScore Football Match Intelligence & Notification Engine.

Scrapes today's football fixtures from SofaScore, detects live scores & goals,
provides interactive Telegram bot commands (/today, /live, /top), and serves
a modern web dashboard with real-time WebSocket updates.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections import defaultdict
from datetime import datetime, timezone

import uvicorn

from api.routes import init_routes
from api.server import create_app
from api.websocket import ws_manager
from config.settings import get_settings
from core.engine import AlertEngine
from monitors.sofascore_monitor import SofaScoreMonitor
from notifiers.telegram_bot import TelegramNotifier
from storage.database import DatabaseManager

# ── Logging setup ────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-8s │ %(name)-22s │ %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("SportCrawl")


def print_banner() -> None:
    """Print ASCII art banner."""
    print(r"""
========================================================================
  ____                  _      ____                     _ 
 / ___| _ __   ___  _ __| |_  / ___|_ __ __ ___      _| |
 \___ \| '_ \ / _ \| '__| __|| |   | '__/ _` \ \ /\ / / |
  ___) | |_) | (_) | |  | |_ | |___| | | (_| |\ V  V /| |
 |____/| .__/ \___/|_|   \__| \____|_|  \__,_| \_/\_/ |_|
       |_|                                                
========================================================================
""")


async def cli_list_today(
    featured_only: bool = False,
    send_telegram: bool = False,
    format_type: str = "both",
) -> None:
    """CLI mode: Open SofaScore, fetch today's fixtures and print cleanly to terminal."""
    print_banner()
    settings = get_settings()
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"🚀 Opening SofaScore to fetch today's football fixtures ({today_str})...\n")

    monitor = SofaScoreMonitor(settings)
    matches = await monitor.fetch_today_matches(today_str)

    if not matches:
        print("❌ No matches found for today.")
        return

    if featured_only:
        matches = [m for m in matches if m["is_featured"]]

    # Group by Tournament
    grouped = defaultdict(list)
    for m in matches:
        t_key = f"{m['category_name']} - {m['tournament_name']}" if m['category_name'] != m['tournament_name'] else m['tournament_name']
        grouped[t_key].append(m)

    print(f"⚽ Found {len(matches)} football games across {len(grouped)} tournaments for today:\n")

    for league, league_matches in grouped.items():
        is_feat = any(m["is_featured"] for m in league_matches)
        prefix = "⭐ " if is_feat else "🏆 "
        print(f"{prefix}\033[1;36m{league}\033[0m ({len(league_matches)} games)")
        print("-" * 65)

        for m in league_matches:
            t_str = m["start_time"].strftime("%H:%M UTC") if m.get("start_time") else "--:--"
            h_score = m["home_score"] if m["home_score"] is not None else ""
            a_score = m["away_score"] if m["away_score"] is not None else ""
            score_str = f"{h_score} - {a_score}" if h_score != "" else "vs"
            
            status = m.get("status_description", "Not started")
            if m.get("status_type") == "inprogress":
                status_color = f"\033[1;31m🔴 LIVE ({m.get('minute') or 'In Play'})\033[0m"
            elif m.get("status_type") == "finished":
                status_color = "\033[1;32m🏁 FT\033[0m"
            else:
                status_color = f"\033[90m🕒 {t_str}\033[0m"

            print(f"  • {m['home_team']:<24} {score_str:^7} {m['away_team']:>24}  [{status_color}]")

        print()

    # If requested, send document to Telegram directly
    if send_telegram:
        db = DatabaseManager(settings.database_url)
        await db.init_db()
        for m in matches:
            await db.upsert_match(m, is_featured=m.get("is_featured", False))
        notifier = TelegramNotifier(
            bot_token=settings.telegram_bot_token,
            admin_chat_id=settings.admin_chat_id,
            db=db,
        )
        print(f"📤 Sending {format_type.upper()} document of today's fixtures to Telegram (Chat ID: {settings.admin_chat_id})...")
        await notifier.send_matches_document(
            chat_id=settings.admin_chat_id,
            format_type=format_type,
            date_str=today_str,
        )
        await notifier.close()
        await db.close()
        print("✅ Document successfully sent to Telegram!")


async def main() -> None:
    """Bootstrap and run the SofaScore Football Bot system."""
    print_banner()
    logger.info("SofaScore Football Match Intelligence — Booting Service")

    # ── 1. Load configuration ────────────────────────────────────────
    settings = get_settings()
    logger.info(
        "Configuration loaded (Featured Competitions: %d)",
        len(settings.featured_leagues) if isinstance(settings.featured_leagues, list) else 1,
    )

    # ── 2. Initialise database ───────────────────────────────────────
    db = DatabaseManager(settings.database_url)
    await db.init_db()

    # ── 3. Initialise monitor ────────────────────────────────────────
    monitor = SofaScoreMonitor(settings)

    # ── 4. Initialise Telegram notifier ──────────────────────────────
    notifier = TelegramNotifier(
        bot_token=settings.telegram_bot_token,
        admin_chat_id=settings.admin_chat_id,
        db=db,
        monitor=monitor,
    )
    # Start Telegram bot command listener
    await notifier.start_polling()

    # ── 5. Initialise engine ─────────────────────────────────────────
    engine = AlertEngine(
        notifier=notifier,
        db=db,
        notify_goals=settings.notify_goal_events,
        notify_kickoff=settings.notify_kickoff_events,
        notify_final=settings.notify_match_ended,
        ws_broadcast=ws_manager.broadcast_alert,
    )

    # Connect monitor to engine
    monitor.on_match(engine.process_match)

    # ── Hook up hourly matches file delivery to Telegram ─────────────
    if settings.send_matches_file_hourly:
        async def handle_cycle_complete(matches: list[dict[str, Any]]) -> None:
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            logger.info("Hourly scrape complete (%d fixtures). Sending %s document to Telegram...", len(matches), settings.matches_file_format.upper())
            await notifier.send_matches_document(
                chat_id=settings.admin_chat_id,
                format_type=settings.matches_file_format,
                date_str=today_str,
            )

        monitor.on_cycle_complete(handle_cycle_complete)
        logger.info("Hourly Telegram document delivery enabled (Format: %s)", settings.matches_file_format.upper())

    # ── 6. Initialise dashboard API ──────────────────────────────────
    app = create_app()
    init_routes(db, engine, settings, monitor=monitor)
    logger.info("Dashboard API & Web UI ready at http://localhost:8000")

    # ── 7. Set up graceful shutdown ──────────────────────────────────
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    # ── 8. Launch background tasks ───────────────────────────────────
    async def run_services() -> None:
        uvicorn_config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=8000,
            log_level="warning",
            access_log=False,
        )
        uvicorn_server = uvicorn.Server(uvicorn_config)

        tasks = [
            asyncio.create_task(engine.start(), name="alert-engine"),
            asyncio.create_task(monitor.start(), name="sofascore-monitor"),
            asyncio.create_task(uvicorn_server.serve(), name="api-server"),
        ]

        await shutdown_event.wait()

        logger.info("Initiating graceful shutdown...")
        await engine.stop()
        await monitor.stop()

        for task in tasks:
            if not task.done():
                task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)

    try:
        await run_services()
    finally:
        logger.info("Cleaning up resources...")
        await notifier.close()
        await db.close()
        logger.info("SofaScore Football Bot shut down cleanly ✓")


def cli_entry() -> None:
    """CLI entry point with argument parsing."""
    parser = argparse.ArgumentParser(
        description="SofaScore Football Bot — Match Intelligence & Telegram Bot"
    )
    parser.add_argument(
        "--list-today",
        "--today",
        "-t",
        action="store_true",
        help="Fetch and list all today's football fixtures directly in terminal and exit",
    )
    parser.add_argument(
        "--top",
        action="store_true",
        help="Filter CLI list to top/featured leagues only",
    )

    parser.add_argument(
        "--send-telegram",
        action="store_true",
        help="Also generate and send the full fixtures document (.txt and .json) to Telegram",
    )
    parser.add_argument(
        "--format",
        choices=["txt", "json", "both"],
        default="both",
        help="Format of the fixtures document to send (txt, json, both)",
    )

    args = parser.parse_args()

    if args.list_today or args.top or args.send_telegram:
        asyncio.run(
            cli_list_today(
                featured_only=args.top,
                send_telegram=args.send_telegram,
                format_type=args.format,
            )
        )
    else:
        asyncio.run(main())


if __name__ == "__main__":
    cli_entry()
