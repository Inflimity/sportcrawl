"""
WebSocket connection manager for real-time football score streaming.

Manages active client connections and broadcasts live goal and status updates.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from fastapi import WebSocket

if TYPE_CHECKING:
    from core.engine import MatchAlert

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages WebSocket connections for live match streaming."""

    def __init__(self) -> None:
        self._connections: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        """Accept and register a new WebSocket connection."""
        await websocket.accept()
        async with self._lock:
            self._connections.append(websocket)
        logger.info(
            "WebSocket client connected (total: %d)", len(self._connections)
        )

    async def disconnect(self, websocket: WebSocket) -> None:
        """Remove a disconnected WebSocket."""
        async with self._lock:
            if websocket in self._connections:
                self._connections.remove(websocket)
        logger.info(
            "WebSocket client disconnected (total: %d)", len(self._connections)
        )

    async def broadcast_alert(self, alert: "MatchAlert") -> None:
        """Broadcast a match alert to all connected WebSocket clients."""
        if not self._connections:
            return

        m = alert.match
        payload = {
            "type": "match_alert",
            "alert_type": alert.alert_type,
            "message": alert.message,
            "data": {
                "match_id": m.match_id,
                "tournament_name": m.tournament_name,
                "category_name": m.category_name,
                "home_team": m.home_team,
                "away_team": m.away_team,
                "home_score": m.home_score,
                "away_score": m.away_score,
                "status_type": m.status_type,
                "status_description": m.status_description,
                "minute": m.minute,
                "start_time": m.start_time.isoformat() if m.start_time else "",
                "sofascore_url": m.sofascore_url,
            },
        }
        message = json.dumps(payload)

        dead_connections = []
        async with self._lock:
            for ws in self._connections:
                try:
                    await ws.send_text(message)
                except Exception:
                    dead_connections.append(ws)

            for ws in dead_connections:
                if ws in self._connections:
                    self._connections.remove(ws)


# Global singleton instance
ws_manager = ConnectionManager()
