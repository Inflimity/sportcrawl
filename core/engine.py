"""
Core Orchestration & Sports Alert Engine for SofaScore Football Bot.

Processes incoming football matches, handles database upserting,
detects live score changes (goals, kickoffs, fulltime), and dispatches
notifications to Telegram and real-time WebSocket clients.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from notifiers.telegram_bot import TelegramNotifier
    from storage.database import DatabaseManager
    from storage.models import FootballMatch

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MatchAlert:
    """Football match alert object passed to notifier and websocket."""

    match: FootballMatch
    alert_type: str  # "new_match", "goal", "kickoff", "halftime", "fulltime"
    message: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class AlertEngine:
    """Central sports match processing and dispatch engine."""

    def __init__(
        self,
        notifier: TelegramNotifier,
        db: DatabaseManager,
        notify_goals: bool = True,
        notify_kickoff: bool = True,
        notify_final: bool = True,
        ws_broadcast=None,
    ) -> None:
        self._notifier = notifier
        self._db = db
        self._notify_goals = notify_goals
        self._notify_kickoff = notify_kickoff
        self._notify_final = notify_final
        self._ws_broadcast = ws_broadcast
        self._running = False
        self._stats = {
            "matches_scanned": 0,
            "live_matches": 0,
            "goals_tracked": 0,
            "dispatched": 0,
        }

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    async def process_match(self, match_data: dict[str, Any]) -> None:
        """Process a single match fixture from SofaScore."""
        self._stats["matches_scanned"] += 1

        match_obj, is_new, score_change = await self._db.upsert_match(
            match_data, is_featured=match_data.get("is_featured", False)
        )

        if match_obj.status_type == "inprogress":
            self._stats["live_matches"] += 1

        # 1. Goal Alert
        if score_change and self._notify_goals and not is_new:
            self._stats["goals_tracked"] += 1
            self._stats["dispatched"] += 1
            logger.info("⚡ %s", score_change)
            
            alert = MatchAlert(
                match=match_obj,
                alert_type="goal",
                message=score_change,
            )
            if self._notifier:
                await self._notifier.send_match_alert(alert)
            if self._ws_broadcast:
                await self._ws_broadcast(alert)

        # 2. Kickoff Alert
        elif match_obj.status_type == "inprogress" and not match_obj.notified_kickoff and self._notify_kickoff:
            if match_obj.is_featured or match_obj.bookmarked:
                match_obj.notified_kickoff = True
                self._stats["dispatched"] += 1
                msg = f"⚽ Match Started: {match_obj.home_team} vs {match_obj.away_team} ({match_obj.tournament_name})"
                alert = MatchAlert(match=match_obj, alert_type="kickoff", message=msg)
                if self._notifier:
                    await self._notifier.send_match_alert(alert)
                if self._ws_broadcast:
                    await self._ws_broadcast(alert)

        # 3. Match Ended Alert
        elif match_obj.status_type == "finished" and not match_obj.notified_final and self._notify_final:
            if match_obj.is_featured or match_obj.bookmarked:
                match_obj.notified_final = True
                score_str = f"{match_obj.home_score or 0} - {match_obj.away_score or 0}"
                msg = f"🏁 Full Time: {match_obj.home_team} {score_str} {match_obj.away_team} ({match_obj.tournament_name})"
                alert = MatchAlert(match=match_obj, alert_type="fulltime", message=msg)
                if self._notifier:
                    await self._notifier.send_match_alert(alert)
                if self._ws_broadcast:
                    await self._ws_broadcast(alert)

    async def start(self) -> None:
        """Start the engine."""
        self._running = True
        logger.info("AlertEngine active — ready to process football match events")

    async def stop(self) -> None:
        """Stop the engine."""
        self._running = False
        logger.info("AlertEngine stopped")
