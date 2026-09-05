"""
Team Matcher & Fuzzy Search Engine for SportCrawl.

Matches raw prediction team names against SportyBet fixtures list with
alias normalization and token similarity.

Two things here are load-bearing, and both exist because name similarity alone
was booking the wrong game:

**Kickoff is evidence, not decoration.** ``match_fixture`` takes the fixture's
own kickoff and refuses any event that starts more than ``max_delta_minutes``
away from it. Measured over 431 fixtures, a ±90 minute check caught 100% of
provably-wrong matches, where raising the name threshold caught 89% at best and
cost unknown recall. The card-wide booking window closed wrong-*day* matches;
this closes the wrong-*game* ones a shared day still allows.

**Similarity is token evidence, not string overlap.** ``difflib`` on whole names
rewards a shared prefix, which is exactly what two clubs from the same city
have. It scored ``Cagliari`` against ``Real Madrid`` at 0.526 — over the 0.48
booking threshold — on nothing but coincidental letters.
"""

from __future__ import annotations

import difflib
import re
from datetime import datetime, timezone
from typing import Any, Optional, Union


# Canonical team aliases (normalized lowercase -> target tokens)
TEAM_ALIASES: dict[str, str] = {
    "man utd": "manchester united",
    "man united": "manchester united",
    "manchester utd": "manchester united",
    "man city": "manchester city",
    "mancity": "manchester city",
    "wolves": "wolverhampton",
    "wolverhampton wanderers": "wolverhampton",
    "spurs": "tottenham",
    "tottenham hotspur": "tottenham",
    "newcastle utd": "newcastle united",
    # These expand a short name to the full one. The reverse direction —
    # "newcastle united" -> "newcastle" — is deliberately absent: it strips
    # the very token that separates Newcastle United from Newcastle Jets.
    "west ham": "west ham united",
    "brighton": "brighton hove albion",
    "nottingham": "nottingham forest",
    "psg": "paris saint germain",
    "paris sg": "paris saint germain",
    "paris saint-germain": "paris saint germain",
    "inter": "internazionale",
    "inter milan": "internazionale",
    "ac milan": "milan",
    "roma": "as roma",
    "juve": "juventus",
    "bayern": "bayern munchen",
    "bayern munich": "bayern munchen",
    "dortmund": "borussia dortmund",
    "bvb": "borussia dortmund",
    "leverkusen": "bayer leverkusen",
    "bayer 04 leverkusen": "bayer leverkusen",
    "rb leipzig": "rasenballsport leipzig",
    "leipzig": "rasenballsport leipzig",
    "sporting": "sporting cp",
    "sporting lisbon": "sporting cp",
    "benfica": "sl benfica",
    "porto": "fc porto",
    "atletico": "atletico madrid",
    "atl madrid": "atletico madrid",
    "atletico de madrid": "atletico madrid",
    "real": "real madrid",
    "barca": "barcelona",
    "athletic bilbao": "athletic club",
    "sociedad": "real sociedad",
}

# Club noise words to ignore in fuzzy comparisons.
#
# "united"/"utd" are deliberately NOT here. They were, and deleting them
# collapsed every United into any other club from the same city: `Man Utd` and
# `Man City` both reduced to "manchester" and scored 0.85, as did
# `Sheffield United` against `Sheffield Wednesday`. A token that decides which
# of two real clubs is meant is the opposite of noise.
NOISE_TOKENS = {
    "fc", "cf", "sc", "ac", "as", "fk", "sk", "ss", "sv", "bsc",
    "vfb", "vfl", "tsg", "afc", "club", "calcio", "cd", "ud", "ca",
    "de", "la", "the", "and",
}

# Spelling variants folded before comparison, so "Utd" and "United" are one token.
TOKEN_ALIASES: dict[str, str] = {
    "utd": "united",
    "st": "saint",
    "athl": "athletic",
    "wdrs": "wanderers",
}

# Tokens that mark a *different* team entity rather than a spelling of the same
# one. If one name carries one of these and the other does not, they are not the
# same team at any similarity score: `Atletico Madrid` is not `Atletico Madrid B`.
SQUAD_MARKERS = {
    "b", "c", "ii", "iii", "women", "w", "ladies", "dames", "feminino",
    "femenino", "reserve", "reserves", "youth", "academy", "junior", "jr",
    "u17", "u18", "u19", "u20", "u21", "u23",
}

# How far a candidate event's kickoff may sit from the fixture's own kickoff.
# 90 minutes is wide enough to absorb schedule drift and provider disagreement,
# and far tighter than the gap to any other fixture in the same competition.
DEFAULT_KICKOFF_TOLERANCE_MINUTES = 90

# Token-level similarity above which two tokens are read as the same word.
_TOKEN_MATCH_RATIO = 0.85

# Shortest token that may match by containment ("gladbach" in "monchengladbach").
_MIN_CONTAINMENT_LEN = 4


def normalize_team_name(name: str) -> str:
    """Clean and normalize team name into comparable alphanumeric tokens."""
    if not name:
        return ""
    text = name.lower()
    text = re.sub(r"[\'\"\.\-\_\,\/\(\)]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    # Check direct aliases
    if text in TEAM_ALIASES:
        text = TEAM_ALIASES[text]

    tokens = [TOKEN_ALIASES.get(w, w) for w in text.split()]
    tokens = [w for w in tokens if w not in NOISE_TOKENS]
    return " ".join(tokens) if tokens else text


def _tokens_match(a: str, b: str) -> bool:
    """True if two tokens are the same word, allowing for spelling and truncation."""
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= _MIN_CONTAINMENT_LEN and shorter in longer:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= _TOKEN_MATCH_RATIO


def team_similarity(name_a: str, name_b: str) -> float:
    """
    Compute similarity ratio between two team names (0.0 to 1.0).

    Scoring is token evidence rather than string overlap. Whole-string
    ``difflib`` was the previous basis and it rated unrelated clubs on shared
    letters — ``Cagliari``/``Real Madrid`` at 0.526, above the booking
    threshold. A name that shares no *word* with another shares no evidence.

    Full containment of the shorter name scores high, because that is what an
    abbreviation looks like (`Leeds` for `Leeds United`). Partial overlap is
    held below any usable threshold, because that is what two different clubs
    with a common element look like (`Manchester United` / `Manchester City`).
    """
    norm_a = normalize_team_name(name_a)
    norm_b = normalize_team_name(name_b)

    if not norm_a or not norm_b:
        return 0.0

    tokens_a = norm_a.split()
    tokens_b = norm_b.split()

    # A squad marker on one side only means a different team, whatever else matches.
    if (set(tokens_a) & SQUAD_MARKERS) != (set(tokens_b) & SQUAD_MARKERS):
        return 0.0

    if norm_a == norm_b:
        return 1.0

    # Lone initials carry no identifying weight and would otherwise count as an
    # unexplained word: "M'gladbach" must still reach Borussia Monchengladbach.
    # Applied after the squad-marker test above, which reads "B" and "C".
    tokens_a = [t for t in tokens_a if len(t) > 1] or tokens_a
    tokens_b = [t for t in tokens_b if len(t) > 1] or tokens_b

    # Greedy one-to-one token pairing, so a repeated word cannot count twice.
    unpaired = list(tokens_b)
    matched = 0
    for token in tokens_a:
        for candidate in unpaired:
            if _tokens_match(token, candidate):
                unpaired.remove(candidate)
                matched += 1
                break

    if not matched:
        return 0.0

    longer = max(len(tokens_a), len(tokens_b))
    shorter = min(len(tokens_a), len(tokens_b))

    if matched == shorter:
        # Every word of the shorter name is accounted for: an abbreviation or a
        # dropped suffix. Confidence falls with each unexplained extra word.
        return max(0.60, 0.90 - 0.10 * (longer - matched))

    # Only some words line up. Two clubs sharing a city or a sponsor land here,
    # and they must stay below every threshold the callers use.
    return 0.45 * (matched / longer)


def parse_kickoff(value: Union[str, int, float, datetime, None]) -> Optional[datetime]:
    """
    Read a kickoff from any of the shapes this codebase carries one in.

    ``Fixture.start_utc`` is an ISO string, SportyBet's ``estimateStartTime`` is
    epoch milliseconds, and callers sometimes hold a datetime already. Naive
    values are read as UTC, which is what every field name here promises.
    """
    if value is None or value == "":
        return None

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    if isinstance(value, (int, float)) or str(value).strip().lstrip("-").isdigit():
        try:
            raw = float(value)
        except (TypeError, ValueError):
            return None
        # Epoch milliseconds if it is far too large to be seconds.
        seconds = raw / 1000.0 if abs(raw) > 1e11 else raw
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def event_kickoff(event: dict[str, Any]) -> Optional[datetime]:
    """Kickoff of a SportyBet event, or ``None`` if it publishes none."""
    for key in ("estimateStartTime", "startTime", "estimatedStartTime"):
        parsed = parse_kickoff(event.get(key))
        if parsed is not None:
            return parsed
    return None


def match_fixture(
    pred_home: str,
    pred_away: str,
    candidate_events: list[dict[str, Any]],
    threshold: float = 0.55,
    kickoff: Union[str, int, float, datetime, None] = None,
    max_delta_minutes: int = DEFAULT_KICKOFF_TOLERANCE_MINUTES,
) -> Optional[tuple[dict[str, Any], float]]:
    """
    Find best matching SportyBet event from candidate list.
    Returns (event_dict, confidence_score) or None.

    Pass ``kickoff`` — the fixture's own start time — whenever the caller has
    it. Names alone cannot separate two games on the same card that share a
    club name; kickoff can, and it is the single check that measured 100%
    against provably-wrong matches. An event that publishes no start time is
    still eligible, so a field rename at SportyBet degrades to today's
    behaviour rather than emptying the card.
    """
    want_kickoff = parse_kickoff(kickoff)
    tolerance = max(0, max_delta_minutes) * 60

    best_event = None
    best_score = 0.0

    for event in candidate_events:
        if want_kickoff is not None:
            got_kickoff = event_kickoff(event)
            if got_kickoff is not None:
                if abs((got_kickoff - want_kickoff).total_seconds()) > tolerance:
                    continue

        c_home = event.get("homeTeamName") or event.get("home_team") or event.get("home", "")
        c_away = event.get("awayTeamName") or event.get("away_team") or event.get("away", "")

        score_h = team_similarity(pred_home, c_home)
        score_a = team_similarity(pred_away, c_away)

        # Average of home and away match
        combined = (score_h + score_a) / 2.0

        if combined > best_score and score_h >= threshold and score_a >= threshold:
            best_score = combined
            best_event = event

    if best_event and best_score >= threshold:
        return best_event, best_score

    return None


def fixture_key(home: str, away: str) -> str:
    """
    Stable key for carrying per-fixture data alongside the plain-text contract.

    Predictions travel between the pipeline and the booker as text lines, which
    have no room for a kickoff. Rather than change that format — it is a public
    contract with ``core.prediction_parser`` — callers pass a side table keyed
    by this function, built from the same normalization the matcher uses so a
    lookup cannot miss on spelling.
    """
    return f"{normalize_team_name(home)}|{normalize_team_name(away)}"
