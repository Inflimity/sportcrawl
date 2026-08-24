"""
Booker Engine for SportCrawl.

Main entrypoint that coordinates prediction text parsing, SportyBet fixture matching,
and betslip booking code generation.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from core.prediction_parser import ParsedBet, parse_prediction_text
from services.sportybet_service import BookingResult, SportyBetBookerService

logger = logging.getLogger("SportCrawl.BookerEngine")


class BookerEngine:
    """Coordinates prediction parsing and automated bet booking."""

    def __init__(self, country_code: str = "ng", headless: bool = True):
        self.country_code = country_code
        self.headless = headless
        self._service = SportyBetBookerService(country_code=country_code, headless=headless)

    @property
    def service(self) -> SportyBetBookerService:
        return self._service

    def parse_predictions(self, raw_text: str) -> list[ParsedBet]:
        """Parse raw user text into structured predictions without executing booking."""
        return parse_prediction_text(raw_text)

    async def book_predictions(
        self,
        raw_text: str,
        timeout_seconds: int = 45,
    ) -> BookingResult:
        """
        Takes raw prediction text, parses the games, visits SportyBet,
        and generates the booking code.
        """
        parsed_bets = self.parse_predictions(raw_text)
        if not parsed_bets:
            return BookingResult(
                success=False,
                error_message="Could not detect any valid match predictions in the provided text. Format examples: 'Arsenal vs Chelsea - Over 2.5', 'Real Madrid vs Barcelona : 1', 'Inter vs Milan -> GG'.",
            )

        logger.info("Parsed %d predictions. Launching SportyBet auto-booker...", len(parsed_bets))
        result = await self._service.generate_booking_code(parsed_bets, timeout_seconds=timeout_seconds)
        return result

    @staticmethod
    def format_telegram_response(result: BookingResult, parsed_bets: list[ParsedBet]) -> str:
        """Format the booking result for Telegram with rich HTML styling and copyable code."""
        if not result.success:
            err = result.error_message or "Unable to generate booking code on SportyBet."
            msg = (
                f"❌ <b>SportyBet Booking Failed</b>\n\n"
                f"⚠️ <i>{err}</i>\n\n"
                f"Parsed {len(parsed_bets)} matches from your text:"
            )
            for b in parsed_bets:
                msg += f"\n• {b.home_team} vs {b.away_team} ➔ <b>{b.selection}</b>"
            return msg

        code = result.booking_code or "N/A"
        odds_str = f"<b>{result.total_odds}</b>" if result.total_odds else "<i>Calculating...</i>"

        lines = [
            "🎟️ <b>SportyBet Booking Code Generated!</b>\n",
            f"📋 Booking Code: <code>{code}</code> <i>(tap to copy)</i>",
            f"📊 Total Odds: {odds_str}",
            f"⚽ Selected Matches: <b>{result.selections_count}</b>\n",
            "<b>✅ Games Added to Slip:</b>",
        ]

        for s in result.booked_selections:
            odds_tag = f" (@{s.odds})" if s.odds else ""
            lines.append(f"• <b>{s.home_team} vs {s.away_team}</b>\n   └ 🎯 <b>{s.selection_desc}</b> ({s.market_desc}){odds_tag}")

        if result.unmatched_selections:
            lines.append("\n<b>⚠️ Unmatched / Not Found:</b>")
            for u in result.unmatched_selections:
                lines.append(f"• <i>{u.home_team} vs {u.away_team}</i> ({u.selection_desc})")

        if result.share_url:
            lines.append(f'\n🔗 <a href="{result.share_url}">Open Betslip on SportyBet ↗</a>')

        return "\n".join(lines)
