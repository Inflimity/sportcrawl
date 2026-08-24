"""
Competition whitelist for SportCrawl's prediction engine.

A raw SofaScore fixture dump is mostly unbettable: youth sides, regional
amateur divisions, women's state leagues and preseason friendlies all arrive
in the same feed as LaLiga. Those fixtures have no reliable form data, and the
lower South American and African tiers additionally carry real
match-manipulation risk, so we do not predict on them at all.

Selection is therefore an explicit allowlist keyed by SofaScore ``category``
(the country/confederation), never a blanket "everything except". Exclusion
patterns are applied on top so that e.g. "LaLiga, Women" is dropped even
though "LaLiga" is allowed.
"""

from __future__ import annotations

import re

# Country/confederation -> substrings that identify a tradeable competition.
# Matching is case-insensitive against the normalized tournament name.
ALLOWED_COMPETITIONS: dict[str, tuple[str, ...]] = {
    "England": ("premier league", "championship", "efl cup", "carabao", "fa cup"),
    "Spain": ("laliga", "la liga", "copa del rey"),
    "Italy": ("serie a", "serie b", "coppa italia"),
    "Germany": ("bundesliga", "dfb pokal"),
    "France": ("ligue 1", "ligue 2", "coupe de france", "trophee des champions"),
    "Netherlands": ("eredivisie", "keuken kampioen"),
    "Portugal": ("liga portugal", "taca de portugal", "taça de portugal"),
    "Belgium": ("pro league",),
    "Scotland": ("premiership", "scottish cup"),
    "Austria": ("bundesliga",),
    "Switzerland": ("super league",),
    "Turkey": ("super lig", "süper lig"),
    "Greece": ("super league",),
    "Denmark": ("superliga",),
    "Norway": ("eliteserien",),
    "Sweden": ("allsvenskan",),
    "Brazil": (
        "brasileirão série a",
        "brasileirao serie a",
        "brasileirão betano",
        "brasileirão série b",
        "brasileirao serie b",
        "brasileirão série c",
        "brasileirao serie c",
        "copa do brasil",
    ),
    "Argentina": ("liga profesional", "primera b nacional", "copa argentina"),
    "Chile": ("liga de primera",),
    "Uruguay": ("primera division",),
    "Colombia": ("primera a",),
    "Mexico": ("liga mx",),
    "USA": ("major league soccer", "mls"),
    "Japan": ("j1 league",),
    "South Korea": ("k league 1",),
    "Europe": ("champions league", "europa league", "conference league"),
}

# Applied after the allowlist. Any hit disqualifies the fixture outright.
EXCLUSION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bu-?\d{2}\b",                       # U17, U20, U-23
        r"\bsub-?\d{2}\b",                     # Sub-19
        r"\byouth\b|\bjunior\b|\bprimavera\b",
        r"\bwomen\b|\bfeminin|\bfemenin|\bladies\b|\bdames\b",
        r"\bfriendly\b|\bfriendlies\b|\bamistoso\b",
        r"\bamateur\b|\bpromocional\b",
        r"\breserve\b|\breserves\b|\bii\b",
        r"\bfutsal\b|\bbeach\b",
    )
)

# Form history is computed from a team's recent matches; these competition
# types are excluded from that history because they distort scoring rates.
FORM_EXCLUSION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bfriendly\b|\bfriendlies\b|\bamistoso\b",
        r"\bu-?\d{2}\b|\bsub-?\d{2}\b|\byouth\b",
    )
)


def normalize(text: str) -> str:
    """Lowercase and collapse whitespace for tolerant substring matching."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def is_excluded(tournament: str) -> bool:
    """True if the competition name matches any hard exclusion pattern."""
    return any(p.search(tournament or "") for p in EXCLUSION_PATTERNS)


def is_form_eligible(tournament: str) -> bool:
    """True if a past match should count toward a team's form baseline."""
    return not any(p.search(tournament or "") for p in FORM_EXCLUSION_PATTERNS)


def is_allowed(category: str, tournament: str) -> bool:
    """
    True if this (country, competition) pair is on the allowlist and survives
    the exclusion patterns.

    ``category`` matters: "Premier League" is a top flight in England and a
    third-tier-quality competition in Burundi, and only the former is tradeable.
    """
    if is_excluded(tournament):
        return False

    patterns = ALLOWED_COMPETITIONS.get(category)
    if not patterns:
        return False

    name = normalize(tournament)
    return any(p in name for p in patterns)
