"""
FastAPI application server for the SofaScore Football Bot web dashboard.

Serves the static frontend, REST API, and WebSocket endpoint.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.routes import router as api_router
from api.websocket import ws_manager

logger = logging.getLogger(__name__)

# Resolve paths
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_STATIC_DIR = _PROJECT_ROOT / "static"


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="SportCrawl — Football Match Intelligence",
        description="Real-time football fixtures, live scores, and tournament tracking",
        version="2.0.0",
    )

    # ── REST API routes ──────────────────────────────────────────────
    app.include_router(api_router)

    # ── WebSocket endpoint ───────────────────────────────────────────
    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        """Live match streaming via WebSocket."""
        await ws_manager.connect(websocket)
        try:
            while True:
                data = await websocket.receive_text()
                if data == "ping":
                    await websocket.send_text("pong")
        except WebSocketDisconnect:
            await ws_manager.disconnect(websocket)
        except Exception:
            await ws_manager.disconnect(websocket)

    # ── Static file serving ──────────────────────────────────────────
    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # ── Serve the dashboard SPA ──────────────────────────────────────
    @app.get("/")
    async def serve_dashboard():
        """Serve the main dashboard HTML."""
        index_path = _STATIC_DIR / "index.html"
        if index_path.exists():
            return FileResponse(str(index_path))
        return {"message": "SofaScore Football API is running. Dashboard not found at /static/index.html"}

    return app
