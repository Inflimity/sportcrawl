"""
Team Matcher & Fuzzy Search Engine for SportCrawl.

Matches raw prediction team names against SportyBet fixtures list with
alias normalization and token similarity.
"""

from __future__ import annotations

import difflib
import re
from typing import Any, Optional


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
    "newcastle utd": "newcastle",
    "newcastle united": "newcastle",
    "west ham": "west ham united",
    "brighton": "brighton hove albion",
    "nottingham": "nottingham forest",
    "sheffield utd": "sheffield united",
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

# Club noise words to ignore in fuzzy comparisons
NOISE_TOKENS = {
    "fc", "cf", "sc", "ac", "as", "fk", "sk", "ss", "sv", "bsc",
    "vfb", "vfl", "tsg", "afc", "club", "calcio", "cd", "ud", "ca",
    "de", "la", "the", "and", "utd", "united"
}


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

    tokens = [w for w in text.split() if w not in NOISE_TOKENS]
    return " ".join(tokens) if tokens else text


def team_similarity(name_a: str, name_b: str) -> float:
    """Compute similarity ratio between two team names (0.0 to 1.0)."""
    norm_a = normalize_team_name(name_a)
    norm_b = normalize_team_name(name_b)

    if not norm_a or not norm_b:
        return 0.0

    if norm_a == norm_b:
        return 1.0

    # Substring check
    if norm_a in norm_b or norm_b in norm_a:
        return 0.85

    # Sequence similarity
    seq_ratio = difflib.SequenceMatcher(None, norm_a, norm_b).ratio()

    # Token overlap check (Jaccard on tokens)
    tokens_a = set(norm_a.split())
    tokens_b = set(norm_b.split())
    if tokens_a and tokens_b:
        token_ratio = len(tokens_a.intersection(tokens_b)) / float(max(len(tokens_a), len(tokens_b)))
        return max(seq_ratio, token_ratio)

    return seq_ratio


def match_fixture(
    pred_home: str,
    pred_away: str,
    candidate_events: list[dict[str, Any]],
    threshold: float = 0.55,
) -> Optional[tuple[dict[str, Any], float]]:
    """
    Find best matching SportyBet event from candidate list.
    Returns (event_dict, confidence_score) or None.
    """
    best_event = None
    best_score = 0.0

    for event in candidate_events:
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
