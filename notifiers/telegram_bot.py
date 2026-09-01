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
from zoneinfo import ZoneInfo

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

if TYPE_CHECKING:
    from core.engine import MatchAlert
    from monitors.sofascore_monitor import SofaScoreMonitor
    from storage.database import DatabaseManager
    from storage.models import FootballMatch

logger = logging.getLogger(__name__)

# Nigerian Timezone (West Africa Time, UTC+1)
LAGOS_TZ = ZoneInfo("Africa/Lagos")


def to_wat(dt: Optional[datetime]) -> Optional[datetime]:
    """Convert a UTC datetime to West Africa Time (WAT / Nigerian Time, UTC+1)."""
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LAGOS_TZ)


def _escape(text: str) -> str:
    """Escape text for Telegram HTML parse mode."""
    return html_lib.escape(str(text)) if text else ""


def _format_match_row(match: FootballMatch) -> str:
    """Format a single match fixture line for Telegram message text in Nigerian Time (WAT)."""
    wat_dt = to_wat(match.start_time)
    t_str = wat_dt.strftime("%H:%M") if wat_dt else "--:--"
    
    if match.status_type == "inprogress":
        status_badge = f"🔴 <b>{match.home_score or 0}-{match.away_score or 0}</b> ({match.minute or 'LIVE'})"
    elif match.status_type == "finished":
        status_badge = f"🏁 <b>{match.home_score or 0}-{match.away_score or 0}</b>"
    elif match.status_type in ("postponed", "canceled"):
        status_badge = f"⚠️ <i>{match.status_description}</i>"
    else:
        status_badge = f"🕒 <i>{t_str} WAT</i>"

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


def generate_matches_txt(
    matches: list[FootballMatch],
    date_str: str,
    title_override: Optional[str] = None,
) -> io.BytesIO:
    """Generate a clean formatted plain-text document of matches for the day in Nigerian Time."""
    output = io.StringIO()
    now_wat = datetime.now(LAGOS_TZ).strftime("%Y-%m-%d %I:%M:%S %p WAT")
    doc_title = title_override or "TODAY'S FOOTBALL FIXTURES & RESULTS"
    output.write("=" * 75 + "\n")
    output.write(f" SPORTCRAWL — {doc_title.upper()} ({date_str})\n")
    output.write(f" Times shown in West Africa Time (WAT / Nigerian Time, UTC+1)\n")
    output.write(f" Total Matches in File: {len(matches)} | Generated: {now_wat}\n")
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
            wat_dt = to_wat(m.start_time)
            t_str = wat_dt.strftime("%H:%M WAT") if wat_dt else "--:--"
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


def generate_matches_json(
    matches: list[FootballMatch],
    date_str: str,
    category_name: str = "all",
) -> io.BytesIO:
    """Generate a structured JSON document containing match data in WAT / UTC."""
    now_wat = datetime.now(LAGOS_TZ).isoformat()
    data = {
        "source": "SportCrawl / SofaScore",
        "date": date_str,
        "filter": category_name,
        "timezone": "Africa/Lagos (WAT / UTC+1)",
        "total_matches": len(matches),
        "generated_at": now_wat,
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
                "start_time_wat": to_wat(m.start_time).strftime("%Y-%m-%d %H:%M:%S WAT") if m.start_time else "",
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
        # Read once here rather than per digest: the ticket-4 flags are read on
        # every scheduled run, and BaseSettings re-parses the .env each call.
        from config.settings import Settings
        self._settings = Settings()

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
            self._app.add_handler(CommandHandler(["upcoming", "fixtures", "schedule"], self._cmd_upcoming))
            self._app.add_handler(CommandHandler(["live"], self._cmd_live))
            self._app.add_handler(CommandHandler(["top", "featured"], self._cmd_top))
            self._app.add_handler(CommandHandler(["export", "file", "download", "json", "txt"], self._cmd_export))
            self._app.add_handler(CommandHandler(["refresh"], self._cmd_refresh))
            self._app.add_handler(CommandHandler(["book", "booker", "slip", "sportybet"], self._cmd_book))
            self._app.add_handler(CommandHandler(["predict", "picks", "bankers", "ai", "top10", "top20"], self._cmd_predict))

            # Callbacks
            self._app.add_handler(CallbackQueryHandler(self._handle_callback))

            # Message text handler for pasted predictions
            self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text_message))

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
            "Track today's fixtures, live scores, and automatically generate <b>Statistical Predictions & SportyBet Booking Codes</b>!\n\n"
            "<b>Available Commands:</b>\n"
            "🎯 /predict — Run statistical screening on today's fixtures & auto-book on SportyBet\n"
            "🎟️ /book — Auto-book copied predictions & get SportyBet booking code\n"
            "📅 /today — View all scheduled games for today\n"
            "🕒 /upcoming — View upcoming matches that have NOT started yet\n"
            "🔴 /live — View currently live in-play games\n"
            "⭐ /top — View Top European & international leagues\n"
            "📁 /export — Download full matches list (.txt / .json)\n"
            "🔄 /refresh — Trigger fresh scrape from SofaScore\n"
            "❓ /help — Show this help message"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🎯 Today's AI Picks & Code", callback_data="btn_predict"),
                InlineKeyboardButton("📅 Today's Games", callback_data="btn_today"),
            ],
            [
                InlineKeyboardButton("🕒 Upcoming Games", callback_data="btn_upcoming"),
                InlineKeyboardButton("🔴 Live Scores", callback_data="btn_live"),
            ],
            [
                InlineKeyboardButton("⭐ Top Leagues", callback_data="btn_top"),
                InlineKeyboardButton("📄 Download TXT", callback_data="btn_export_txt"),
            ],
        ])
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )

    async def _cmd_upcoming(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handler for /upcoming and /fixtures command (games that haven't started yet)."""
        if not update.effective_message or not self._db:
            return

        today_str = datetime.now(LAGOS_TZ).strftime("%Y-%m-%d")
        all_matches = await self._db.get_matches_for_date(today_str)

        if not all_matches and self._monitor:
            status_msg = await update.effective_message.reply_text("🔄 Fetching today's fixtures from SofaScore...")
            raw_matches = await self._monitor.fetch_today_matches(today_str)
            for m in raw_matches:
                await self._db.upsert_match(m, is_featured=m.get("is_featured", False))
            all_matches = await self._db.get_matches_for_date(today_str)
            try:
                await status_msg.delete()
            except Exception:
                pass

        upcoming_matches = [m for m in all_matches if m.status_type == "notstarted"]

        if not upcoming_matches:
            await update.effective_message.reply_text(
                f"ℹ️ No upcoming unstarted matches found for today ({today_str}). All matches may have finished or are in play."
            )
            return

        featured_upcoming = [m for m in upcoming_matches if m.is_featured]
        display_matches = featured_upcoming if featured_upcoming else upcoming_matches[:25]

        chunks = format_matches_message(
            display_matches,
            f"🕒 Upcoming Matches ({len(display_matches)} of {len(upcoming_matches)} unstarted games today)"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"🕒 Download Upcoming TXT ({len(upcoming_matches)})", callback_data="btn_export_upcoming_txt"),
                InlineKeyboardButton("📊 JSON", callback_data="btn_export_upcoming_json"),
            ],
            [
                InlineKeyboardButton(f"📄 Download All {len(all_matches)} (.txt)", callback_data="btn_export_txt"),
                InlineKeyboardButton("🔴 Live Only", callback_data="btn_live"),
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

    async def _cmd_today(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handler for /today command."""
        if not update.effective_message or not self._db:
            return

        today_str = datetime.now(LAGOS_TZ).strftime("%Y-%m-%d")
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

        upcoming_count = sum(1 for m in all_matches if m.status_type == "notstarted")
        live_count = sum(1 for m in all_matches if m.status_type == "inprogress")

        chunks = format_matches_message(
            display_matches,
            f"📅 Today's Top Leagues ({len(display_matches)} of {len(all_matches)} total games worldwide)"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"📄 All Games TXT ({len(all_matches)})", callback_data="btn_export_txt"),
                InlineKeyboardButton(f"🕒 Upcoming TXT ({upcoming_count})", callback_data="btn_export_upcoming_txt"),
            ],
            [
                InlineKeyboardButton(f"🔴 Live TXT ({live_count})", callback_data="btn_export_live_txt"),
                InlineKeyboardButton("📊 Full JSON", callback_data="btn_export_json"),
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

        cmd_text = update.effective_message.text.lower() if update.effective_message.text else ""
        today_str = datetime.now(LAGOS_TZ).strftime("%Y-%m-%d")

        matches = await self._db.get_matches_for_date(today_str)

        if not matches and self._monitor:
            status_msg = await update.effective_message.reply_text("🔄 Database empty — fetching today's matches from SofaScore...")
            raw_matches = await self._monitor.fetch_today_matches(today_str)
            for m in raw_matches:
                await self._db.upsert_match(m, is_featured=m.get("is_featured", False))
            matches = await self._db.get_matches_for_date(today_str)
            try:
                await status_msg.delete()
            except Exception:
                pass

        if not matches:
            await update.effective_message.reply_text("❌ No matches available to export for today.")
            return

        # Check for specific filter in command args (e.g. "/export upcoming" or "/export live")
        fmt = "txt" if "txt" in cmd_text else ("json" if "json" in cmd_text else "both")

        if "upcoming" in cmd_text or "fixture" in cmd_text:
            upcoming = [m for m in matches if m.status_type == "notstarted"]
            await self.send_matches_document(
                chat_id=update.effective_chat.id,
                format_type=fmt,
                date_str=today_str,
                matches=upcoming,
                doc_type="upcoming",
            )
            return

        if "live" in cmd_text:
            live = [m for m in matches if m.status_type == "inprogress"]
            await self.send_matches_document(
                chat_id=update.effective_chat.id,
                format_type=fmt,
                date_str=today_str,
                matches=live,
                doc_type="live",
            )
            return

        # Send full export
        await self.send_matches_document(
            chat_id=update.effective_chat.id,
            format_type=fmt,
            date_str=today_str,
            matches=matches,
            doc_type="all",
        )

    async def _cmd_live(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handler for /live command."""
        if not update.effective_message or not self._db:
            return

        live_matches = await self._db.get_live_matches()
        chunks = format_matches_message(live_matches, "🔴 Live Football Matches Now")
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"🔴 Download Live TXT ({len(live_matches)})", callback_data="btn_export_live_txt"),
                InlineKeyboardButton("📊 Live JSON", callback_data="btn_export_live_json"),
            ],
            [
                InlineKeyboardButton("🕒 Upcoming Games", callback_data="btn_upcoming"),
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

        today_str = datetime.now(LAGOS_TZ).strftime("%Y-%m-%d")
        matches = await self._db.get_matches_for_date(today_str, featured_only=True)
        chunks = format_matches_message(matches, f"⭐ Top Leagues & Featured Games ({today_str})")
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📅 All Matches", callback_data="btn_today"),
                InlineKeyboardButton("🕒 Upcoming", callback_data="btn_upcoming"),
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
        today_str = datetime.now(LAGOS_TZ).strftime("%Y-%m-%d")
        matches = await self._monitor.fetch_today_matches(today_str)
        for m in matches:
            await self._db.upsert_match(m, is_featured=m.get("is_featured", False))

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📅 View Today", callback_data="btn_today"),
                InlineKeyboardButton("🕒 Upcoming", callback_data="btn_upcoming"),
                InlineKeyboardButton("📄 Download TXT", callback_data="btn_export_txt"),
            ]
        ])
        await status_msg.edit_text(
            f"✅ Refreshed! Found {len(matches)} matches for today ({today_str} WAT).",
            reply_markup=keyboard,
        )

    async def _cmd_book(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handler for /book command to auto-generate SportyBet booking code."""
        if not update.effective_message:
            return

        text = update.effective_message.text or ""
        # Strip command prefix
        lines = text.split("\n")
        first_line = lines[0]
        match_cmd = re.match(r"^/([a-zA-Z0-9_]+)\s*(.*)$", first_line)
        if match_cmd:
            rest_of_first_line = match_cmd.group(2).strip()
            remaining_lines = "\n".join([rest_of_first_line] + lines[1:]).strip()
            raw_input = remaining_lines
        else:
            raw_input = text.strip()

        if not raw_input:
            help_msg = (
                "🎟️ <b>SportyBet Auto-Booker</b>\n\n"
                "Paste your betting predictions below or use <code>/book [games]</code>.\n\n"
                "<b>Supported Format Examples:</b>\n"
                "• <code>Arsenal vs Chelsea - Over 2.5</code>\n"
                "• <code>Real Madrid vs Barcelona : 1</code>\n"
                "• <code>Inter Milan vs Juventus -> GG</code>\n"
                "• <code>Man City vs Liverpool | 1X</code>\n"
                "• <code>Aston Villa vs Wolves: DNB 1</code>\n\n"
                "<i>Just paste any multi-line tips and I will visit SportyBet, build the slip, and return the booking code for you!</i>"
            )
            await update.effective_message.reply_text(help_msg, parse_mode=ParseMode.HTML)
            return

        await self._process_booking_request(update, raw_input)

    async def _handle_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Auto-detect pasted prediction blocks in regular chat messages."""
        if not update.effective_message or not update.effective_message.text:
            return

        msg_text = update.effective_message.text.strip()
        if len(msg_text) < 6:
            return

        # Check if text looks like a prediction list
        from core.prediction_parser import parse_prediction_text
        parsed = parse_prediction_text(msg_text)
        if parsed and len(parsed) >= 1:
            await self._process_booking_request(update, msg_text)

    async def _process_booking_request(self, update: Update, text: str) -> None:
        """Execute booking flow and send formatted result."""
        from core.booker_engine import BookerEngine
        from core.prediction_parser import parse_prediction_text

        parsed_bets = parse_prediction_text(text)
        if not parsed_bets:
            await update.effective_message.reply_text(
                "⚠️ <b>Could not recognize any matches or markets</b>\n\n"
                "Please make sure your text contains team names separated by <code>vs</code> or <code>-</code> and the market (e.g. <code>Arsenal vs Chelsea - Over 2.5</code>).",
                parse_mode=ParseMode.HTML,
            )
            return

        preview_msg = f"⏳ <i>Recognized {len(parsed_bets)} games. Visiting SportyBet to generate booking code...</i>"
        status_msg = await update.effective_message.reply_text(preview_msg, parse_mode=ParseMode.HTML)

        try:
            engine = BookerEngine(country_code="ng", headless=True)
            result = await engine.book_predictions(text)
            response_text = BookerEngine.format_telegram_response(result, parsed_bets)

            keyboard = None
            if result.success and result.share_url:
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔗 Open Betslip on SportyBet", url=result.share_url)]
                ])

            await status_msg.edit_text(
                response_text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.error("Error processing telegram booking request: %s", e, exc_info=True)
            await status_msg.edit_text(
                f"❌ <b>Error:</b> <i>{html_lib.escape(str(e))}</i>",
                parse_mode=ParseMode.HTML,
            )

    async def _cmd_predict(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handler for /predict command — runs statistical screening and auto-books on SportyBet."""
        if not update.effective_message:
            return

        status_msg = await update.effective_message.reply_text(
            "🧠 <i>Analyzing today's fixtures & team forms with Poisson statistical model...</i>",
            parse_mode=ParseMode.HTML,
        )

        try:
            today_str = datetime.now(LAGOS_TZ).strftime("%Y-%m-%d")
            raw_matches = []

            # 1. Re-scrape first, then read the DB back.
            #
            # This used to prefer the stored card whenever the DB held anything
            # for today, and only scrape when it held nothing. The stored card
            # goes stale: the upcoming-fixtures sweep does not revisit a match
            # once it has kicked off, so its status_type stays "notstarted"
            # long after full time and a 21:00 /predict was screening a 15:00
            # game. filter_fixtures now drops those on kickoff time, but a
            # stale card also *misses* fixtures added since the last poll, and
            # no downstream filter can recover those. So refresh here.
            if self._monitor:
                try:
                    scraped = await self._monitor.fetch_today_matches(today_str)
                    if self._db and scraped:
                        for m in scraped:
                            await self._db.upsert_match(m, is_featured=m.get("is_featured", False))
                    if not self._db:
                        raw_matches = scraped
                except Exception as e:
                    # A failed refresh is not a failed prediction — fall through
                    # to whatever the DB holds and let the kickoff cutoff keep
                    # played fixtures out.
                    logger.warning("Fixture refresh before /predict failed: %s", e)

            if self._db:
                db_matches = await self._db.get_matches_for_date(today_str)
                if db_matches:
                    from services.pipeline import convert_matches_to_raw_dicts
                    raw_matches = convert_matches_to_raw_dicts(db_matches)

            if not raw_matches:
                await status_msg.edit_text(
                    f"⚠️ No matches found for today ({today_str}). Try running /refresh first.",
                    parse_mode=ParseMode.HTML,
                )
                return

            await status_msg.edit_text(
                f"🧠 <i>Screening {len(raw_matches)} fixtures & auto-booking top selections on SportyBet...</i>",
                parse_mode=ParseMode.HTML,
            )

            # 2. Check desired tier from command arguments or command name
            cmd_text = ""
            args_list = []
            if update.message and update.message.text:
                parts = update.message.text.strip().split()
                cmd_text = parts[0].lower()
                args_list = parts[1:]
            elif context and context.args:
                args_list = context.args

            req_n = None
            if "top10" in cmd_text or ("10" in args_list):
                req_n = 10
            elif "top20" in cmd_text or ("20" in args_list):
                req_n = 20

            from services.pipeline import PredictionBookingPipeline
            pipeline = PredictionBookingPipeline(country_code="ng", headless=True)

            if req_n in (10, 20):
                # Single tier request
                result = await pipeline.run_pipeline(raw_matches, top_n=req_n, auto_book=True)
                title = f"🎯 Today's Top {req_n} Banker Predictions ({today_str})"
                text_response = PredictionBookingPipeline.format_telegram_digest(result, title)
                keyboard_rows = []
                if result.booking_result and result.booking_result.success and result.booking_result.share_url:
                    keyboard_rows.append([InlineKeyboardButton(f"🔗 Open Top {req_n} Betslip on SportyBet", url=result.booking_result.share_url)])
                keyboard_rows.append([
                    InlineKeyboardButton("🎯 Top 10", callback_data="btn_predict_10"),
                    InlineKeyboardButton("🚀 Top 20", callback_data="btn_predict_20"),
                ])
                keyboard = InlineKeyboardMarkup(keyboard_rows)
            else:
                # Default /predict: generate Top 10, Top 20 and the draw ladder
                dual_res = await pipeline.run_dual_pipeline(
                    raw_matches, auto_book=True, include_draws=True,
                    include_two_odds=self._settings.two_odds_enabled,
                    two_odds_cap=self._settings.two_odds_cap,
                    two_odds_max_legs=self._settings.two_odds_max_legs,
                    two_odds_min_legs=self._settings.two_odds_min_legs,
                    two_odds_source=self._settings.two_odds_source,
                    two_odds_short_window=self._settings.two_odds_short_window,
                    two_odds_markets=self._settings.two_odds_markets,
                    two_odds_per_fixture=self._settings.two_odds_per_fixture
                )
                text_response = PredictionBookingPipeline.format_telegram_dual_digest(dual_res, today_str)
                keyboard_rows = []
                links = []
                if dual_res.tier_10.booking_result and dual_res.tier_10.booking_result.success and dual_res.tier_10.booking_result.share_url:
                    links.append(InlineKeyboardButton("🔗 Top 10 Betslip", url=dual_res.tier_10.booking_result.share_url))
                if dual_res.tier_20.booking_result and dual_res.tier_20.booking_result.success and dual_res.tier_20.booking_result.share_url:
                    links.append(InlineKeyboardButton("🔗 Top 20 Betslip", url=dual_res.tier_20.booking_result.share_url))
                if dual_res.two_odds and dual_res.two_odds.booking_result and dual_res.two_odds.booking_result.success and dual_res.two_odds.booking_result.share_url:
                    links.append(InlineKeyboardButton("💰 2 Odds Betslip", url=dual_res.two_odds.booking_result.share_url))
                if links:
                    keyboard_rows.append(links)
                keyboard_rows.append([
                    InlineKeyboardButton("🎯 Top 10 Only", callback_data="btn_predict_10"),
                    InlineKeyboardButton("🚀 Top 20 Only", callback_data="btn_predict_20"),
                ])
                keyboard = InlineKeyboardMarkup(keyboard_rows)

            await status_msg.edit_text(
                text_response,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.error("Error executing /predict command: %s", e, exc_info=True)
            await status_msg.edit_text(
                f"❌ <b>Prediction failed:</b> <i>{html_lib.escape(str(e))}</i>",
                parse_mode=ParseMode.HTML,
            )

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle inline button clicks."""
        query = update.callback_query
        if not query or not query.data:
            return
        await query.answer()

        data = query.data
        today_str = datetime.now(LAGOS_TZ).strftime("%Y-%m-%d")

        if data == "btn_predict":
            await self._cmd_predict(update, context)
        elif data == "btn_predict_10":
            if context:
                context.args = ["10"]
            await self._cmd_predict(update, context)
        elif data == "btn_predict_20":
            if context:
                context.args = ["20"]
            await self._cmd_predict(update, context)
        elif data == "btn_today":
            await self._cmd_today(update, context)
        elif data == "btn_upcoming":
            await self._cmd_upcoming(update, context)
        elif data == "btn_live":
            await self._cmd_live(update, context)
        elif data == "btn_top":
            await self._cmd_top(update, context)
        elif data == "btn_refresh":
            await self._cmd_refresh(update, context)
        elif data in ("btn_export_upcoming_txt", "btn_export_upcoming_json"):
            fmt = "txt" if "txt" in data else "json"
            all_m = await self._db.get_matches_for_date(today_str) if self._db else []
            upcoming = [m for m in all_m if m.status_type == "notstarted"]
            await self.send_matches_document(
                chat_id=update.effective_chat.id,
                format_type=fmt,
                date_str=today_str,
                matches=upcoming,
                doc_type="upcoming",
            )
        elif data in ("btn_export_live_txt", "btn_export_live_json"):
            fmt = "txt" if "txt" in data else "json"
            live = await self._db.get_live_matches() if self._db else []
            await self.send_matches_document(
                chat_id=update.effective_chat.id,
                format_type=fmt,
                date_str=today_str,
                matches=live,
                doc_type="live",
            )
        elif data in ("btn_export_txt", "btn_export_json"):
            fmt = "txt" if data == "btn_export_txt" else "json"
            matches = await self._db.get_matches_for_date(today_str) if self._db else []
            if not matches and self._monitor and self._db:
                raw_matches = await self._monitor.fetch_today_matches(today_str)
                for m in raw_matches:
                    await self._db.upsert_match(m, is_featured=m.get("is_featured", False))
                matches = await self._db.get_matches_for_date(today_str)
            await self.send_matches_document(
                chat_id=update.effective_chat.id,
                format_type=fmt,
                date_str=today_str,
                matches=matches,
                doc_type="all",
            )

    async def send_matches_document(
        self,
        chat_id: int,
        format_type: str = "both",
        date_str: Optional[str] = None,
        matches: Optional[list[FootballMatch]] = None,
        doc_type: str = "all",
    ) -> None:
        """Generate and send TXT and/or JSON fixtures document to Telegram chat."""
        if not date_str:
            date_str = datetime.now(LAGOS_TZ).strftime("%Y-%m-%d")

        if matches is None and self._db:
            matches = await self._db.get_matches_for_date(date_str)

        if not matches:
            logger.info("No matches found to send as document for %s", date_str)
            return

        if doc_type == "upcoming":
            title = "Upcoming Football Fixtures"
            prefix = "upcoming"
            emoji = "🕒"
        elif doc_type == "live":
            title = "Live In-Play Football Matches"
            prefix = "live"
            emoji = "🔴"
        elif doc_type == "top":
            title = "Top Leagues & Featured Matches"
            prefix = "top"
            emoji = "⭐"
        else:
            title = "Today's Football Fixtures & Results"
            prefix = "matches"
            emoji = "📄"

        # 1. Send TXT file
        if format_type in ("txt", "both"):
            try:
                txt_bio = generate_matches_txt(matches, date_str, title_override=title)
                filename = f"sportcrawl_{prefix}_{date_str}.txt"
                caption = f"{emoji} <b>SportCrawl {title}</b> ({date_str})\n⚽ Total Fixtures: <b>{len(matches)}</b> (WAT)"
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
                json_bio = generate_matches_json(matches, date_str, category_name=doc_type)
                filename = f"sportcrawl_{prefix}_{date_str}.json"
                caption = f"📊 <b>SportCrawl {title} (JSON)</b> ({date_str})\n⚽ Total Fixtures: <b>{len(matches)}</b> (WAT)"
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

    async def send_custom_message(
        self,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: Any = None,
    ) -> None:
        """Send custom formatted message to the admin chat."""
        try:
            p_mode = ParseMode.HTML if parse_mode.upper() == "HTML" else None
            await self._bot.send_message(
                chat_id=self._admin_chat_id,
                text=text,
                parse_mode=p_mode,
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )
        except Exception as e:
            logger.warning("Failed to send custom message to Telegram: %s", e)

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
        """Send scheduled digest: a single concise overview message + full TXT/JSON file attachments."""
        if not self._db:
            return
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        all_matches = await self._db.get_matches_for_date(today_str)

        if not all_matches:
            logger.info("No matches available for scheduled digest on %s", today_str)
            return

        total_count = len(all_matches)
        live_count = sum(1 for m in all_matches if m.status_type == "inprogress")
        finished_count = sum(1 for m in all_matches if m.status_type == "finished")
        upcoming_count = sum(1 for m in all_matches if m.status_type == "notstarted")
        featured_count = sum(1 for m in all_matches if m.is_featured)

        # 1. Send single clean digest card (1 message only to avoid rate limits)
        summary_text = (
            f"<b>{title}</b> ({today_str})\n\n"
            f"📊 <b>Today's Fixtures Snapshot:</b>\n"
            f"• ⚽ Total Matches Worldwide: <b>{total_count}</b>\n"
            f"• 🔴 In-Play / Live Now: <b>{live_count}</b>\n"
            f"• 🏁 Finished Games: <b>{finished_count}</b>\n"
            f"• 🕒 Upcoming Fixtures: <b>{upcoming_count}</b>\n"
            f"• ⭐ Top League Games: <b>{featured_count}</b>\n\n"
            f"📁 <i>Complete match list, live scores, and links are attached below in the file export:</i>"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"📄 Download All {total_count} Games (.txt)", callback_data="btn_export_txt"),
            ],
            [
                InlineKeyboardButton("📊 Download JSON", callback_data="btn_export_json"),
                InlineKeyboardButton("🔴 Live Only", callback_data="btn_live"),
            ]
        ])

        try:
            await self._bot.send_message(
                chat_id=self._admin_chat_id,
                text=summary_text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.warning("Failed to send scheduled digest summary card: %s", e)

        # 2. Automatically generate and send Top 10 & Top 20 AI Banker Picks & SportyBet Booking Codes
        try:
            from services.pipeline import PredictionBookingPipeline, convert_matches_to_raw_dicts
            raw_matches = convert_matches_to_raw_dicts(all_matches)
            pipeline = PredictionBookingPipeline(country_code="ng", headless=True)
            # include_draws adds the experimental draw ladder as a third ticket
            # block, built from the same fixtures and forms as Top 10/20.
            dual_res = await pipeline.run_dual_pipeline(
                raw_matches, auto_book=True, include_draws=True,
                    include_two_odds=self._settings.two_odds_enabled,
                    two_odds_cap=self._settings.two_odds_cap,
                    two_odds_max_legs=self._settings.two_odds_max_legs,
                    two_odds_min_legs=self._settings.two_odds_min_legs,
                    two_odds_source=self._settings.two_odds_source,
                    two_odds_short_window=self._settings.two_odds_short_window,
                    two_odds_markets=self._settings.two_odds_markets,
                    two_odds_per_fixture=self._settings.two_odds_per_fixture
            )

            if dual_res.tier_10.picks or dual_res.tier_20.picks:
                predict_msg = PredictionBookingPipeline.format_telegram_dual_digest(dual_res, today_str)
                links = []
                if dual_res.tier_10.booking_result and dual_res.tier_10.booking_result.success and dual_res.tier_10.booking_result.share_url:
                    links.append(InlineKeyboardButton("🔗 Top 10 Betslip", url=dual_res.tier_10.booking_result.share_url))
                if dual_res.tier_20.booking_result and dual_res.tier_20.booking_result.success and dual_res.tier_20.booking_result.share_url:
                    links.append(InlineKeyboardButton("🔗 Top 20 Betslip", url=dual_res.tier_20.booking_result.share_url))
                if dual_res.two_odds and dual_res.two_odds.booking_result and dual_res.two_odds.booking_result.success and dual_res.two_odds.booking_result.share_url:
                    links.append(InlineKeyboardButton("💰 2 Odds Betslip", url=dual_res.two_odds.booking_result.share_url))
                pred_kb = InlineKeyboardMarkup([links]) if links else None

                await self._bot.send_message(
                    chat_id=self._admin_chat_id,
                    text=predict_msg,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=pred_kb,
                )
                logger.info("Sent scheduled Top 10 & Top 20 AI picks and booking codes to Telegram")
        except Exception as e:
            logger.warning("Failed to generate scheduled AI predictions in digest: %s", e)

        # 3. Attach full TXT / JSON document attachments directly
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
