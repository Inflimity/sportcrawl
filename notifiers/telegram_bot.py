"""
Telegram Bot Notifier for SportCrawl / SofaScore Football Matches.

Provides interactive commands:
- /today or /games — List of today's football fixtures grouped by league
- /live — Currently live matches with live scores and minutes
- /top — Featured / Top European leagues and competitions
- /export or /file — Download full matches list as formatted .txt or .json file
- /refresh — Force refresh fixtures from SofaScore
- /help — Command reference & bot status

Supports rich inline keyboards, document attachments (.txt / .json), match bookmarking, and goal notifications.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import io
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

if TYPE_CHECKING:
    from core.engine import MatchAlert
    from monitors.sofascore_monitor import SofaScoreMonitor
    from storage.database import DatabaseManager
    from storage.models import FootballMatch

logger = logging.getLogger(__name__)


def _escape(text: str) -> str:
    """Escape text for Telegram HTML parse mode."""
    return html_lib.escape(str(text)) if text else ""


def _format_match_row(match: FootballMatch) -> str:
    """Format a single match fixture line for Telegram message text."""
    t_str = match.start_time.strftime("%H:%M")
    
    if match.status_type == "inprogress":
        status_badge = f"🔴 <b>{match.home_score or 0}-{match.away_score or 0}</b> ({match.minute or 'LIVE'})"
    elif match.status_type == "finished":
        status_badge = f"🏁 <b>{match.home_score or 0}-{match.away_score or 0}</b>"
    elif match.status_type in ("postponed", "canceled"):
        status_badge = f"⚠️ <i>{match.status_description}</i>"
    else:
        status_badge = f"🕒 <i>{t_str} UTC</i>"

    link = f'<a href="{match.sofascore_url}">{_escape(match.home_team)} vs {_escape(match.away_team)}</a>' if match.sofascore_url else f"{_escape(match.home_team)} vs {_escape(match.away_team)}"
    return f"• {link}\n   └ {status_badge}"


def format_matches_message(matches: list[FootballMatch], title: str = "📅 Today's Football Fixtures") -> list[str]:
    """Group matches by tournament and split into Telegram message chunks (<= 4000 chars)."""
    if not matches:
        return [f"<b>{title}</b>\n\nNo matches found for today."]

    grouped: dict[str, list[FootballMatch]] = defaultdict(list)
    for m in matches:
        key = f"{m.category_name} - {m.tournament_name}" if m.category_name and m.category_name != m.tournament_name else m.tournament_name
        grouped[key].append(m)

    chunks: list[str] = []
    current_chunk = f"⚽ <b>{title}</b> ({len(matches)} matches)\n\n"

    for league, league_matches in grouped.items():
        league_section = f"🏆 <b>{_escape(league)}</b>\n"
        for m in league_matches:
            league_section += _format_match_row(m) + "\n"
        league_section += "\n"

        if len(current_chunk) + len(league_section) > 3800:
            chunks.append(current_chunk.strip())
            current_chunk = league_section
        else:
            current_chunk += league_section

    if current_chunk.strip():
        chunks.append(current_chunk.strip())

    return chunks


def generate_matches_txt(matches: list[FootballMatch], date_str: str) -> io.BytesIO:
    """Generate a clean formatted plain-text document of all matches for the day."""
    output = io.StringIO()
    output.write("=" * 75 + "\n")
    output.write(f" SPORTCRAWL — TODAY'S FOOTBALL FIXTURES & RESULTS ({date_str})\n")
    output.write(f" Total Matches: {len(matches)} | Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n")
    output.write("=" * 75 + "\n\n")

    grouped: dict[str, list[FootballMatch]] = defaultdict(list)
    for m in matches:
        key = f"{m.category_name} - {m.tournament_name}" if m.category_name and m.category_name != m.tournament_name else m.tournament_name
        grouped[key].append(m)

    for league, league_matches in grouped.items():
        is_feat = any(m.is_featured for m in league_matches)
        prefix = "[FEATURED] " if is_feat else ""
        output.write(f"=== {prefix}{league.upper()} ({len(league_matches)} matches) ===\n")
        
        for m in league_matches:
            t_str = m.start_time.strftime("%H:%M UTC") if m.start_time else "--:--"
            h_score = m.home_score if m.home_score is not None else ""
            a_score = m.away_score if m.away_score is not None else ""
            score_str = f"{h_score} - {a_score}" if h_score != "" else "vs"
            
            status = m.status_description
            if m.status_type == "inprogress":
                status = f"LIVE ({m.minute or 'In Play'})"
            elif m.status_type == "finished":
                status = "FT"
            else:
                status = f"Kickoff {t_str}"

            output.write(f"  • Match ID: {m.match_id}\n")
            output.write(f"    Teams:    {m.home_team} {score_str} {m.away_team}\n")
            output.write(f"    Status:   {status}\n")
            if m.home_score_ht is not None and m.away_score_ht is not None:
                output.write(f"    HT Score: {m.home_score_ht} - {m.away_score_ht}\n")
            if m.sofascore_url:
                output.write(f"    URL:      {m.sofascore_url}\n")
            output.write("\n")
        output.write("\n")

    bio = io.BytesIO(output.getvalue().encode("utf-8"))
    bio.seek(0)
    return bio


def generate_matches_json(matches: list[FootballMatch], date_str: str) -> io.BytesIO:
    """Generate a structured JSON document containing full match data."""
    data = {
        "source": "SportCrawl / SofaScore",
        "date": date_str,
        "total_matches": len(matches),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "matches": [
            {
                "match_id": m.match_id,
                "slug": m.slug,
                "tournament": m.tournament_name,
                "category": m.category_name,
                "round": m.round_info,
                "is_featured": m.is_featured,
                "home_team": {
                    "id": m.home_team_id,
                    "name": m.home_team,
                    "score": m.home_score,
                    "score_ht": m.home_score_ht,
                },
                "away_team": {
                    "id": m.away_team_id,
                    "name": m.away_team,
                    "score": m.away_score,
                    "score_ht": m.away_score_ht,
                },
                "start_time_utc": m.start_time.isoformat() if m.start_time else "",
                "status_type": m.status_type,
                "status_description": m.status_description,
                "minute": m.minute,
                "sofascore_url": m.sofascore_url,
                "bookmarked": m.bookmarked,
            }
            for m in matches
        ],
    }
    json_bytes = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    bio = io.BytesIO(json_bytes)
    bio.seek(0)
    return bio


class TelegramNotifier:
    """Sends football updates, file attachments, and handles interactive bot commands."""

    def __init__(
        self,
        bot_token: str,
        admin_chat_id: int,
        db: Optional[DatabaseManager] = None,
        monitor: Optional[SofaScoreMonitor] = None,
    ) -> None:
        self._bot_token = bot_token
        self._admin_chat_id = admin_chat_id
        self._db = db
        self._monitor = monitor

        request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
        self._bot = Bot(token=bot_token, request=request)
        self._app: Optional[Application] = None

    def set_dependencies(self, db: DatabaseManager, monitor: SofaScoreMonitor) -> None:
        """Inject database and monitor dependencies."""
        self._db = db
        self._monitor = monitor

    async def start_polling(self) -> None:
        """Start listening for Telegram commands and inline button callbacks."""
        try:
            self._app = Application.builder().token(self._bot_token).build()

            # Commands
            self._app.add_handler(CommandHandler(["start", "help"], self._cmd_help))
            self._app.add_handler(CommandHandler(["today", "games", "matches"], self._cmd_today))
            self._app.add_handler(CommandHandler(["live"], self._cmd_live))
            self._app.add_handler(CommandHandler(["top", "featured"], self._cmd_top))
            self._app.add_handler(CommandHandler(["export", "file", "download", "json", "txt"], self._cmd_export))
            self._app.add_handler(CommandHandler(["refresh"], self._cmd_refresh))

            # Callbacks
            self._app.add_handler(CallbackQueryHandler(self._handle_callback))

            await self._app.initialize()
            await self._app.start()
            await self._app.updater.start_polling(drop_pending_updates=True)
            logger.info("Telegram Bot command handlers started successfully")
        except Exception:
            logger.exception("Failed to start Telegram Bot polling")

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handler for /start and /help commands."""
        if not update.effective_message:
            return
        text = (
            "🤖 <b>Welcome to SportCrawl Football Bot!</b>\n\n"
            "Track today's football fixtures, live scores, and major European leagues in real-time.\n\n"
            "<b>Available Commands:</b>\n"
            "📅 /today — View all scheduled games for today\n"
            "🔴 /live — View currently live in-play games\n"
            "⭐ /top — View Top 5 European Leagues & UCL games\n"
            "📁 /export — Download full matches list as .txt or .json\n"
            "🔄 /refresh — Trigger fresh scrape from SofaScore\n"
            "❓ /help — Show this help message"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📅 Today's Games", callback_data="btn_today"),
                InlineKeyboardButton("🔴 Live Scores", callback_data="btn_live"),
            ],
            [
                InlineKeyboardButton("📄 Download TXT", callback_data="btn_export_txt"),
                InlineKeyboardButton("📊 Download JSON", callback_data="btn_export_json"),
            ],
            [
                InlineKeyboardButton("⭐ Top Leagues", callback_data="btn_top"),
                InlineKeyboardButton("🔄 Refresh Now", callback_data="btn_refresh"),
            ]
        ])
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )

    async def _cmd_today(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handler for /today command."""
        if not update.effective_message or not self._db:
            return

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        matches = await self._db.get_matches_for_date(today_str)

        # If DB is empty, try an immediate fetch
        if not matches and self._monitor:
            status_msg = await update.effective_message.reply_text("🔄 Opening SofaScore to fetch today's fixtures...")
            raw_matches = await self._monitor.fetch_today_matches(today_str)
            for m in raw_matches:
                await self._db.upsert_match(m, is_featured=m.get("is_featured", False))
            matches = await self._db.get_matches_for_date(today_str)
            try:
                await status_msg.delete()
            except Exception:
                pass

        all_matches = matches
        featured_matches = [m for m in all_matches if m.is_featured]
        display_matches = featured_matches if featured_matches else all_matches[:30]

        chunks = format_matches_message(
            display_matches,
            f"📅 Today's Top Leagues ({len(display_matches)} of {len(all_matches)} total games worldwide)"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"📄 Download All {len(all_matches)} Games (.txt)", callback_data="btn_export_txt"),
            ],
            [
                InlineKeyboardButton("📊 Download Full JSON", callback_data="btn_export_json"),
                InlineKeyboardButton("🔴 Live Only", callback_data="btn_live"),
            ],
            [
                InlineKeyboardButton("🔄 Refresh SofaScore", callback_data="btn_refresh"),
            ]
        ])

        for i, chunk in enumerate(chunks):
            reply_markup = keyboard if i == len(chunks) - 1 else None
            await update.effective_message.reply_text(
                chunk,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )

    async def _cmd_export(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handler for /export /file /json /txt commands."""
        if not update.effective_message or not self._db:
            return

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        matches = await self._db.get_matches_for_date(today_str)

        if not matches and self._monitor:
            raw_matches = await self._monitor.fetch_today_matches(today_str)
            for m in raw_matches:
                await self._db.upsert_match(m, is_featured=m.get("is_featured", False))
            matches = await self._db.get_matches_for_date(today_str)

        if not matches:
            await update.effective_message.reply_text("❌ No matches available to export for today.")
            return

        cmd_text = update.effective_message.text.lower() if update.effective_message.text else ""
        fmt = "txt" if "txt" in cmd_text else ("json" if "json" in cmd_text else "both")

        await self.send_matches_document(
            chat_id=update.effective_chat.id,
            format_type=fmt,
            date_str=today_str,
            matches=matches,
        )

    async def _cmd_live(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handler for /live command."""
        if not update.effective_message or not self._db:
            return

        live_matches = await self._db.get_live_matches()
        chunks = format_matches_message(live_matches, "🔴 Live Football Matches Now")
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📅 All Today", callback_data="btn_today"),
                InlineKeyboardButton("🔄 Refresh", callback_data="btn_live"),
            ]
        ])
        for i, chunk in enumerate(chunks):
            reply_markup = keyboard if i == len(chunks) - 1 else None
            await update.effective_message.reply_text(
                chunk,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )

    async def _cmd_top(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handler for /top featured leagues."""
        if not update.effective_message or not self._db:
            return

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        matches = await self._db.get_matches_for_date(today_str, featured_only=True)
        chunks = format_matches_message(matches, f"⭐ Top Leagues & Featured Games ({today_str})")
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📅 All Matches", callback_data="btn_today"),
                InlineKeyboardButton("📄 Download TXT", callback_data="btn_export_txt"),
            ]
        ])
        for i, chunk in enumerate(chunks):
            reply_markup = keyboard if i == len(chunks) - 1 else None
            await update.effective_message.reply_text(
                chunk,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )

    async def _cmd_refresh(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handler for /refresh command."""
        if not update.effective_message or not self._monitor or not self._db:
            return

        status_msg = await update.effective_message.reply_text("🔄 Scraping latest fixtures from SofaScore...")
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        matches = await self._monitor.fetch_today_matches(today_str)
        for m in matches:
            await self._db.upsert_match(m, is_featured=m.get("is_featured", False))

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📅 View Today", callback_data="btn_today"),
                InlineKeyboardButton("📄 Download TXT", callback_data="btn_export_txt"),
                InlineKeyboardButton("📊 JSON", callback_data="btn_export_json"),
            ]
        ])
        await status_msg.edit_text(
            f"✅ Refreshed! Found {len(matches)} matches for today ({today_str}).",
            reply_markup=keyboard,
        )

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle inline button clicks."""
        query = update.callback_query
        if not query or not query.data:
            return
        await query.answer()

        data = query.data
        if data == "btn_today":
            await self._cmd_today(update, context)
        elif data == "btn_live":
            await self._cmd_live(update, context)
        elif data == "btn_top":
            await self._cmd_top(update, context)
        elif data == "btn_refresh":
            await self._cmd_refresh(update, context)
        elif data == "btn_export_txt":
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            matches = await self._db.get_matches_for_date(today_str) if self._db else []
            await self.send_matches_document(
                chat_id=update.effective_chat.id,
                format_type="txt",
                date_str=today_str,
                matches=matches,
            )
        elif data == "btn_export_json":
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            matches = await self._db.get_matches_for_date(today_str) if self._db else []
            await self.send_matches_document(
                chat_id=update.effective_chat.id,
                format_type="json",
                date_str=today_str,
                matches=matches,
            )

    async def send_matches_document(
        self,
        chat_id: int,
        format_type: str = "both",
        date_str: Optional[str] = None,
        matches: Optional[list[FootballMatch]] = None,
    ) -> None:
        """Generate and send TXT and/or JSON fixtures document to Telegram chat."""
        if not date_str:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if matches is None and self._db:
            matches = await self._db.get_matches_for_date(date_str)

        if not matches:
            logger.info("No matches found to send as document for %s", date_str)
            return

        # 1. Send TXT file
        if format_type in ("txt", "both"):
            try:
                txt_bio = generate_matches_txt(matches, date_str)
                filename = f"sportcrawl_matches_{date_str}.txt"
                caption = f"📄 <b>SportCrawl Matches List</b> ({date_str})\n⚽ Total Fixtures: <b>{len(matches)}</b>"
                await self._bot.send_document(
                    chat_id=chat_id,
                    document=txt_bio,
                    filename=filename,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                )
                logger.info("Sent %s to chat %d", filename, chat_id)
            except Exception as e:
                logger.error("Failed to send TXT matches document: %s", e)

        # 2. Send JSON file
        if format_type in ("json", "both"):
            try:
                json_bio = generate_matches_json(matches, date_str)
                filename = f"sportcrawl_matches_{date_str}.json"
                caption = f"📊 <b>SportCrawl Full JSON Data</b> ({date_str})\n⚽ Total Fixtures: <b>{len(matches)}</b>"
                await self._bot.send_document(
                    chat_id=chat_id,
                    document=json_bio,
                    filename=filename,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                )
                logger.info("Sent %s to chat %d", filename, chat_id)
            except Exception as e:
                logger.error("Failed to send JSON matches document: %s", e)

    async def send_match_alert(self, alert: MatchAlert) -> None:
        """Send a real-time event alert (Goal, Kickoff, Fulltime) to the admin chat."""
        try:
            m = alert.match
            t_name = _escape(m.tournament_name)
            h_team = _escape(m.home_team)
            a_team = _escape(m.away_team)
            h_score = m.home_score if m.home_score is not None else 0
            a_score = m.away_score if m.away_score is not None else 0

            if alert.alert_type == "goal":
                title = "⚡ <b>GOAL ALERT!</b>"
            elif alert.alert_type == "kickoff":
                title = "⚽ <b>MATCH KICKOFF</b>"
            elif alert.alert_type == "fulltime":
                title = "🏁 <b>FULL TIME RESULT</b>"
            else:
                title = "📢 <b>MATCH UPDATE</b>"

            text = (
                f"{title}\n\n"
                f"🏆 <b>{t_name}</b>\n"
                f"<b>{h_team}  {h_score} - {a_score}  {a_team}</b>\n"
                f"⏱️ Status: {m.status_description}\n"
            )
            if m.sofascore_url:
                text += f'\n<a href="{m.sofascore_url}">View on SofaScore ↗</a>'

            await self._bot.send_message(
                chat_id=self._admin_chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning("Failed to send Telegram match alert: %s", e)

    async def send_daily_digest(
        self,
        title: str = "🌅 Today's Football Matches Digest",
        send_files: bool = True,
        format_type: str = "both",
    ) -> None:
        """Send scheduled digest with top fixtures and attached TXT/JSON files."""
        if not self._db:
            return
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        all_matches = await self._db.get_matches_for_date(today_str)
        featured_matches = [m for m in all_matches if m.is_featured]
        matches_to_show = featured_matches if featured_matches else all_matches

        if not matches_to_show:
            logger.info("No matches available for scheduled digest on %s", today_str)
            return

        # 1. Send formatted text summary
        chunks = format_matches_message(matches_to_show, f"{title} ({today_str})")
        for chunk in chunks:
            try:
                await self._bot.send_message(
                    chat_id=self._admin_chat_id,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.warning("Failed to send scheduled digest message: %s", e)

        # 2. Send TXT / JSON document attachments
        if send_files:
            await self.send_matches_document(
                chat_id=self._admin_chat_id,
                format_type=format_type,
                date_str=today_str,
                matches=all_matches,
            )

    async def close(self) -> None:
        """Shut down Telegram application and updater."""
        if self._app:
            try:
                if self._app.updater and self._app.updater.running:
                    await self._app.updater.stop()
                if self._app.running:
                    await self._app.stop()
                await self._app.shutdown()
            except Exception as e:
                logger.warning("Error closing Telegram application: %s", e)
        logger.info("TelegramNotifier closed")
