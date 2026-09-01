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

    # ── Key Scheduled Daily Digest Windows (08:00, 12:00, 17:00 WAT) ────
    # Each window re-scrapes SofaScore before screening, so every digest is
    # built on fixtures that have not kicked off yet. The old 22:00 slot is
    # gone: by then most of the day's card has been played.
    daily_digest_enabled: bool = True
    daily_digest_hours: str | list[int] = [8, 12, 17]  # Morning, Midday, Evening
    send_digest_files: bool = True  # Attach .txt / .json file during scheduled digests
    matches_file_format: str = "both"  # "txt", "json", or "both"
    app_timezone: str = "Africa/Lagos"  # West Africa Time (WAT / UTC+1) - Nigerian Time

    # ── Ticket 4: the capped "2 odds" banker ────────────────────────────
    # On by default. Costs NO extra SofaScore requests: the short form window
    # is cut from the events already fetched for Top 10/20.
    # Set TWO_ODDS_ENABLED=false to switch it off without a deploy.
    two_odds_enabled: bool = True
    two_odds_cap: float = 2.0            # max combined odds for the ticket
    two_odds_max_legs: int = 3
    two_odds_min_legs: int = 2           # never ship a one-game "accumulator"
    two_odds_source: str = "form"        # "form" (recent form guide) or "model"
    two_odds_short_window: int = 5       # matches per side the form read uses
    # Markets the form read may choose between, best-supported first. Over 1.5
    # leads because it is a superset of Over 2.5 and so is never less likely —
    # the same match needs two goals rather than three. Drop a market here to
    # ban it from the ticket without a deploy, e.g.
    # TWO_ODDS_MARKETS="Over 1.5,GG,1,2,X" to exclude Over 2.5 entirely.
    two_odds_markets: str | list[str] = ["Over 1.5", "GG", "Over 2.5", "1", "2", "X"]
    # How many of those markets each fixture offers the ticket builder. More
    # alternatives per fixture cost no extra SportyBet calls (markets are
    # cached per event) and let a fixture contribute at a shorter line instead
    # of being dropped when its best read is priced beyond the cap.
    two_odds_per_fixture: int = 3

    # ── Tickets 1 & 2: price awareness ──────────────────────────────────
    # The Top 10/20 path books on model probability alone and never sees a
    # price. These wire the price in. All three default to INERT — the price
    # is shown in the digest but does not change what is selected — because
    # edge is `model probability - implied probability` and inherits every
    # bias in the model probability. Over 1.5 now dominates both tickets and
    # its hit rate has never been graded, so a min-edge filter would currently
    # wave through precisely the picks it exists to catch.
    #
    # Turn these on once `python -m tools.backtest_markets` has produced a
    # measured Over 1.5 rate. Suggested first values once it has:
    #   TOP_RANK_BY_EDGE=true  TOP_MIN_EDGE=0.03  TOP_MAX_PER_MARKET=6
    top_rank_by_edge: bool = False       # rank on edge instead of probability
    top_min_edge: float = 0.0            # drop picks below this edge (0 = off)
    top_max_per_market: int = 0          # max legs of one market (0 = no cap)
    top_pool_depth: int = 30             # how many picks get priced

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
        "two_odds_markets",
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
