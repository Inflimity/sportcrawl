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

# Tier 1: Top 5 European Flights & UEFA Club Competitions
ELITE_COMPETITIONS: dict[str, tuple[str, ...]] = {
    "England": ("premier league",),
    "Spain": ("laliga", "la liga", "primera division"),
    "Italy": ("serie a",),
    "Germany": ("bundesliga",),
    "France": ("ligue 1",),
    "Europe": ("champions league", "europa league", "conference league", "uefa super cup"),
}

# Tier 2: Major Secondary European & Top Americas / Asian Flights
MAJOR_COMPETITIONS: dict[str, tuple[str, ...]] = {
    "England": ("championship", "fa cup", "efl cup", "carabao"),
    "Spain": ("copa del rey", "segunda division", "laliga 2", "la liga 2"),
    "Italy": ("serie b", "coppa italia"),
    "Germany": ("2. bundesliga", "dfb pokal"),
    "France": ("ligue 2", "coupe de france"),
    "Netherlands": ("eredivisie", "knvb beker"),
    "Portugal": ("liga portugal", "primeira liga", "taca de portugal", "taça de portugal"),
    "Belgium": ("pro league", "first division a"),
    "Scotland": ("premiership", "scottish cup"),
    "Turkey": ("super lig", "süper lig", "turkish cup"),
    "Saudi Arabia": ("saudi pro league", "pro league"),
    "USA": ("major league soccer", "mls"),
    "Brazil": ("brasileirão série a", "brasileirao serie a", "brasileirão betano", "copa do brasil"),
    "Argentina": ("liga profesional", "copa de la liga profesional", "copa argentina"),
    "South America": ("copa libertadores", "copa sudamericana"),
    "Mexico": ("liga mx",),
    "Japan": ("j1 league",),
    "South Korea": ("k league 1",),
}

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
        r"\bsrl\b|\bsimulated\b|\breality\b",  # Simulated Reality Leagues
        r"\besport|\befootball\b|\bcyber\b|\bvirtual\b", # Virtual/eSports
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


def competition_tier(category: str, tournament: str) -> int:
    """
    Return priority tier for a competition:
      Tier 1 (Top European 5 Leagues + UEFA Champions/Europa/Conference League) -> 1
      Tier 2 (Championship, Eredivisie, Liga Portugal, MLS, Brasileirao, Cups, etc.) -> 2
      Tier 3 (Other global competitive leagues) -> 3
    """
    cat = (category or "").strip()
    tourn = normalize(tournament)

    # 1. Check Tier 1 Elite
    for country, patterns in ELITE_COMPETITIONS.items():
        if country.lower() in cat.lower() or cat.lower() in country.lower():
            if any(p in tourn for p in patterns):
                return 1

    # 2. Check Tier 2 Major
    for country, patterns in MAJOR_COMPETITIONS.items():
        if country.lower() in cat.lower() or cat.lower() in country.lower():
            if any(p in tourn for p in patterns):
                return 2

    # 3. Direct tournament name checks
    if any(k in tourn for k in ("champions league", "premier league", "serie a", "la liga", "laliga", "bundesliga", "ligue 1")):
        return 1

    if any(k in tourn for k in ("eredivisie", "super lig", "süper lig", "primeira liga", "pro league", "primera division", "mls", "brasileirão", "brasileirao")):
        return 2

    return 3
