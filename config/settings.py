"""
SportCrawl Settings — Central configuration loaded from environment variables.

Uses pydantic-settings for type-safe validation with .env file support.
"""

from __future__ import annotations

from typing import Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Telegram Bot (Alerts & Interactive Commands) ─────────────────────
    telegram_bot_token: str
    admin_chat_id: int

    # ── SofaScore Scraping & Monitoring ─────────────────────────────────
    sofascore_poll_interval_seconds: int = 7200  # 2 hours interval
    sofascore_headless: bool = True
    sofascore_timeout_ms: int = 30000

    # ── Key Scheduled Daily Digest Windows (Option C: 08:00, 15:00, 22:00)
    daily_digest_enabled: bool = True
    daily_digest_hours: str | list[int] = [8, 15, 22]  # Morning (08:00), Afternoon (15:00), Night (22:00)
    send_digest_files: bool = True  # Attach .txt / .json file during scheduled digests
    matches_file_format: str = "both"  # "txt", "json", or "both"

    # ── Top / Featured Leagues & Competitions ────────────────────────────
    # Matches in these tournaments are prioritized in digests & alerts
    featured_leagues: str | list[str] = [
        "Premier League",
        "UEFA Champions League",
        "UEFA Europa League",
        "UEFA Conference League",
        "LaLiga",
        "Serie A",
        "Bundesliga",
        "Ligue 1",
        "FA Cup",
        "EFL Cup",
        "Brasileirão Betano",
        "Major League Soccer",
        "World Cup",
        "European Championship",
        "Copa América",
        "Africa Cup of Nations",
        "Community Shield",
        "Trophee des Champions",
        "Supercopa",
    ]

    # ── Live Score Alert Notifications (Disabled by default to prevent chat spam)
    notify_goal_events: bool = False
    notify_kickoff_events: bool = False
    notify_match_ended: bool = False

    # ── Deduplication & Cache ───────────────────────────────────────────
    redis_url: Optional[str] = None
    dedup_ttl_seconds: int = 86400  # 24h deduplication window

    # ── Database ────────────────────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///./ginNews.sqlite"

    # ── Validators ──────────────────────────────────────────────────────

    @field_validator(
        "featured_leagues",
        mode="before",
    )
    @classmethod
    def split_comma_separated(cls, v: str | list[str]) -> list[str]:
        """Accept comma-separated strings from .env and split into lists."""
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return [item.strip() for item in v if item.strip()]

    @field_validator(
        "daily_digest_hours",
        mode="before",
    )
    @classmethod
    def parse_digest_hours(cls, v: str | list[int] | list[str]) -> list[int]:
        """Parse comma-separated hour numbers e.g. '8,15,22' into a list of ints."""
        if isinstance(v, str):
            return [int(item.strip()) for item in v.split(",") if item.strip().isdigit()]
        return [int(item) for item in v]


def get_settings() -> Settings:
    """Factory function — creates and caches a Settings instance."""
    return Settings()  # type: ignore[call-arg]
