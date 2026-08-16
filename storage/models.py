"""
SQLAlchemy ORM models for SofaScore Football Bot data persistence.

Defines the schema for football matches, tournaments, live score snapshots, and bookmarked games.
Uses SQLAlchemy 2.0 declarative style with async compatibility.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all models."""

    pass


class FootballMatch(Base):
    """Persisted football match / fixture from SofaScore."""

    __tablename__ = "football_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    
    # ── Tournament & Category ────────────────────────────────────────────
    tournament_name: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    category_name: Mapped[str] = mapped_column(String(100), nullable=False, default="International", index=True)
    round_info: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    is_featured: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)

    # ── Teams ────────────────────────────────────────────────────────────
    home_team: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    home_team_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    away_team: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    away_team_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # ── Match Timing ─────────────────────────────────────────────────────
    start_timestamp: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    match_date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)  # YYYY-MM-DD

    # ── Status & Scores ──────────────────────────────────────────────────
    status_type: Mapped[str] = mapped_column(String(50), nullable=False, default="notstarted", index=True) # notstarted, inprogress, finished, postponed, canceled
    status_description: Mapped[str] = mapped_column(String(50), nullable=False, default="Not started")
    home_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    away_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    home_score_ht: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    away_score_ht: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    minute: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # ── Details & Links ──────────────────────────────────────────────────
    sofascore_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    raw_data: Mapped[str] = mapped_column(Text, nullable=False, default="{}") # JSON dump

    # ── User Action Flags ────────────────────────────────────────────────
    bookmarked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notified_kickoff: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notified_final: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:
        score_str = f"{self.home_score}-{self.away_score}" if self.home_score is not None else "vs"
        return (
            f"<FootballMatch {self.match_id}: [{self.tournament_name}] "
            f"{self.home_team} {score_str} {self.away_team} ({self.status_description})>"
        )


class SavedMatch(Base):
    """User bookmarked match for dedicated alerts / tracking."""

    __tablename__ = "saved_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False, index=True)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
