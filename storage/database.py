"""
Async database manager for SofaScore Football Bot.

Handles SQLite connection pool, table creation, upserting match fixtures,
and querying matches by date, tournament, status, or bookmark.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import delete, desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from storage.models import Base, FootballMatch, SavedMatch

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Async database manager backed by SQLAlchemy + aiosqlite."""

    def __init__(self, database_url: str) -> None:
        self._engine = create_async_engine(
            database_url,
            echo=False,
            connect_args={"check_same_thread": False} if "sqlite" in database_url else {},
        )
        self._session_factory = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def init_db(self) -> None:
        """Create all tables if they don't exist."""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables initialised successfully")

    async def upsert_match(self, match_data: dict[str, Any], is_featured: bool = False) -> tuple[FootballMatch, bool, Optional[str]]:
        """
        Upsert a match parsed from SofaScore.
        Returns (match_obj, is_new, score_change_summary_if_any).
        """
        match_id = match_data["match_id"]
        score_change = None

        async with self._session_factory() as session:
            stmt = select(FootballMatch).where(FootballMatch.match_id == match_id)
            result = await session.execute(stmt)
            existing = result.scalars().first()

            if existing is None:
                match = FootballMatch(
                    match_id=match_id,
                    slug=match_data.get("slug", ""),
                    tournament_name=match_data.get("tournament_name", "Unknown Tournament"),
                    category_name=match_data.get("category_name", "International"),
                    round_info=match_data.get("round_info", ""),
                    is_featured=is_featured or match_data.get("is_featured", False),
                    home_team=match_data.get("home_team", "Home Team"),
                    home_team_id=match_data.get("home_team_id"),
                    away_team=match_data.get("away_team", "Away Team"),
                    away_team_id=match_data.get("away_team_id"),
                    start_timestamp=match_data.get("start_timestamp", 0),
                    start_time=match_data.get("start_time", datetime.now(timezone.utc)),
                    match_date=match_data.get("match_date", datetime.now(timezone.utc).strftime("%Y-%m-%d")),
                    status_type=match_data.get("status_type", "notstarted"),
                    status_description=match_data.get("status_description", "Not started"),
                    home_score=match_data.get("home_score"),
                    away_score=match_data.get("away_score"),
                    home_score_ht=match_data.get("home_score_ht"),
                    away_score_ht=match_data.get("away_score_ht"),
                    minute=match_data.get("minute"),
                    sofascore_url=match_data.get("sofascore_url", ""),
                    raw_data=json.dumps(match_data.get("raw", {})),
                )
                session.add(match)
                await session.commit()
                await session.refresh(match)
                return match, True, None

            # Detect score change or status change
            old_h = existing.home_score
            old_a = existing.away_score
            new_h = match_data.get("home_score")
            new_a = match_data.get("away_score")

            if (new_h is not None or new_a is not None) and (old_h != new_h or old_a != new_a):
                score_change = f"GOAL! {existing.home_team} {new_h or 0} - {new_a or 0} {existing.away_team}"

            # Update existing
            existing.tournament_name = match_data.get("tournament_name", existing.tournament_name)
            existing.category_name = match_data.get("category_name", existing.category_name)
            existing.round_info = match_data.get("round_info", existing.round_info)
            existing.is_featured = is_featured or match_data.get("is_featured", existing.is_featured)
            existing.status_type = match_data.get("status_type", existing.status_type)
            existing.status_description = match_data.get("status_description", existing.status_description)
            existing.home_score = new_h
            existing.away_score = new_a
            existing.home_score_ht = match_data.get("home_score_ht", existing.home_score_ht)
            existing.away_score_ht = match_data.get("away_score_ht", existing.away_score_ht)
            existing.minute = match_data.get("minute", existing.minute)
            existing.sofascore_url = match_data.get("sofascore_url", existing.sofascore_url)
            existing.updated_at = datetime.now(timezone.utc)

            await session.commit()
            await session.refresh(existing)
            return existing, False, score_change

    async def get_matches_for_date(
        self,
        date_str: str,
        featured_only: bool = False,
        status: Optional[str] = None,
        league: Optional[str] = None,
    ) -> list[FootballMatch]:
        """Fetch matches scheduled for a given date (YYYY-MM-DD)."""
        async with self._session_factory() as session:
            stmt = select(FootballMatch).where(FootballMatch.match_date == date_str)
            if featured_only:
                stmt = stmt.where(FootballMatch.is_featured == True)
            if status:
                stmt = stmt.where(FootballMatch.status_type == status)
            if league:
                stmt = stmt.where(FootballMatch.tournament_name.ilike(f"%{league}%"))
            stmt = stmt.order_by(
                FootballMatch.is_featured.desc(),
                FootballMatch.start_timestamp.asc(),
                FootballMatch.tournament_name.asc(),
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_live_matches(self) -> list[FootballMatch]:
        """Fetch currently live/in-progress matches."""
        async with self._session_factory() as session:
            stmt = (
                select(FootballMatch)
                .where(FootballMatch.status_type == "inprogress")
                .order_by(
                    FootballMatch.is_featured.desc(),
                    FootballMatch.start_timestamp.asc(),
                )
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_match_by_id(self, match_id: int) -> Optional[FootballMatch]:
        """Get a single match by SofaScore match_id."""
        async with self._session_factory() as session:
            stmt = select(FootballMatch).where(FootballMatch.match_id == match_id)
            result = await session.execute(stmt)
            return result.scalars().first()

    async def toggle_bookmark(self, match_id: int, chat_id: int = 0) -> bool:
        """Toggle bookmark flag for a match. Returns new bookmark state."""
        async with self._session_factory() as session:
            stmt = select(FootballMatch).where(FootballMatch.match_id == match_id)
            result = await session.execute(stmt)
            match = result.scalars().first()
            if not match:
                return False

            match.bookmarked = not match.bookmarked
            new_state = match.bookmarked

            # Track in SavedMatch table
            if new_state and chat_id:
                saved = SavedMatch(match_id=match_id, chat_id=chat_id)
                session.add(saved)
            elif not new_state and chat_id:
                del_stmt = delete(SavedMatch).where(
                    SavedMatch.match_id == match_id, SavedMatch.chat_id == chat_id
                )
                await session.execute(del_stmt)

            await session.commit()
            return new_state

    async def get_bookmarked_matches(self) -> list[FootballMatch]:
        """Get all bookmarked matches."""
        async with self._session_factory() as session:
            stmt = (
                select(FootballMatch)
                .where(FootballMatch.bookmarked == True)
                .order_by(FootballMatch.start_timestamp.asc())
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_all_tournaments_today(self, date_str: str) -> list[str]:
        """Get list of distinct tournament names playing on date."""
        async with self._session_factory() as session:
            stmt = (
                select(FootballMatch.tournament_name)
                .where(FootballMatch.match_date == date_str)
                .distinct()
                .order_by(FootballMatch.tournament_name.asc())
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def close(self) -> None:
        """Dispose of the database engine."""
        await self._engine.dispose()
        logger.info("Database connection closed")
