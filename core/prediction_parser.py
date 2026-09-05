"""
Prediction Text Parser for SportCrawl.

Extracts football matches, market types, and predicted outcomes from
arbitrary raw text (e.g. from Telegram tipsters, betting channels, or AI).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class MarketCategory(str, Enum):
    MATCH_WINNER = "1X2"              # 1, X, 2 / Home, Draw, Away
    DOUBLE_CHANCE = "DOUBLE_CHANCE"    # 1X, 12, X2
    OVER_UNDER = "OVER_UNDER"          # Over/Under X.5 Goals
    CORNERS = "CORNERS"                # Over/Under X.5 Corners
    CORNERS_1X2 = "CORNERS_1X2"        # Most corners: Home / Draw / Away
    BTTS = "BTTS"                      # Both Teams to Score (GG / NG)
    DRAW_NO_BET = "DNB"                # Draw No Bet (Home / Away)
    HANDICAP = "HANDICAP"              # European / Asian Handicap
    CORRECT_SCORE = "CORRECT_SCORE"    # e.g. 2-1, 1-0
    HALFTIME_FULLTIME = "HT_FT"        # e.g. 1/1, X/1, 2/2
    UNKNOWN = "UNKNOWN"


@dataclass
class ParsedBet:
    raw_line: str
    home_team: str
    away_team: str
    market_category: MarketCategory
    selection: str          # Normalized selection (e.g., "1", "X", "2", "Over 2.5", "GG", "1X")
    specifier: Optional[str] = None  # e.g. "total=2.5"
    odds: Optional[float] = None
    confidence: float = 1.0
    # The fixture's own kickoff, when the caller knew it. Set after parsing —
    # the text line carries no time — and used by the booker to reject an event
    # that shares a club name but kicks off at a different hour.
    kickoff: Optional[str] = None
    # Exactly what was written after the fixture, before this parser tried to
    # classify it. The normalized `selection` above only exists for the handful
    # of markets with a fast path; everything else — Correct Score, HT/FT,
    # handicaps, team totals, Early Goals — is resolved by matching this text
    # against the market descriptions on the live event.
    raw_selection: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "raw_line": self.raw_line,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "market_category": self.market_category.value,
            "selection": self.selection,
            "specifier": self.specifier,
            "odds": self.odds,
            "confidence": self.confidence,
            "kickoff": self.kickoff,
            "raw_selection": self.raw_selection,
        }


# Regular expressions for cleaning and tokenizing
EMOJI_PATTERN = re.compile(
    "["
    "\U0001F1E0-\U0001F1FF"  # flags
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F700-\U0001F77F"  # alchemical
    "\U0001F780-\U0001F7FF"  # Geometric Shapes Extended
    "\U0001F800-\U0001F8FF"  # Supplemental Arrows-C
    "\U0001F900-\U0001F9FF"  # Supplemental Symbols and Pictographs
    "\U0001FA00-\U0001FA6F"  # Chess Symbols
    "\U0001FA70-\U0001FAFF"  # Symbols and Pictographs Extended-A
    "\U00002702-\U000027B0"  # Dingbats
    "\U000024C2-\U0001F251"
    "]+",
    flags=re.UNICODE,
)

ODDS_PATTERN = re.compile(r"(?:@\s*|odds?[:\s]+|\[\s*)([1-9]\d{0,1}\.\d{1,2})\b", re.IGNORECASE)
LEADING_NUMBER_PATTERN = re.compile(r"^\s*(?:\d+[\.\)\-:]|\([0-9]+\)|[•\-\*\+])\s*")
TIME_PATTERN = re.compile(r"\b\d{1,2}[:\.]\d{2}(?:\s*(?:am|pm|wat|gmt|utc|cet))?\b", re.IGNORECASE)

# Subjects an "Over/Under N" line can be about. Goals is the default only when
# the line names no subject at all — never as a catch-all for an unknown one.
_CORNERS_RE = re.compile(r"\bCORNERS?\b", re.IGNORECASE)
_OU_SUBJECT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("corners", _CORNERS_RE),
    ("goals", re.compile(r"\bGOALS?\b", re.IGNORECASE)),
    # Recognised so they are refused explicitly rather than read as goals.
    ("cards", re.compile(r"\bCARDS?\b|\bBOOKINGS?\b", re.IGNORECASE)),
    ("shots", re.compile(r"\bSHOTS?\b", re.IGNORECASE)),
    ("fouls", re.compile(r"\bFOULS?\b", re.IGNORECASE)),
    ("offsides", re.compile(r"\bOFFSIDES?\b", re.IGNORECASE)),
    ("throw-ins", re.compile(r"\bTHROW[\s-]*INS?\b", re.IGNORECASE)),
)


# The Over/Under expression itself, and the subject words that legitimately
# accompany it. Whatever survives removing both is a qualifier.
_OU_EXPR_RE = re.compile(r"\b(OVER|UNDER|O|U)[\s/]*[0-9]+(?:\.[0-9]+)?\b", re.IGNORECASE)
_SUBJECT_WORDS_RE = re.compile(
    r"\bGOALS?\b|\bCORNERS?\b|\bCARDS?\b|\bBOOKINGS?\b|\bSHOTS?\b|\bFOULS?\b"
    r"|\bOFFSIDES?\b|\bTHROW[\s-]*INS?\b|\bTOTAL\b|[^\w\s]",
    re.IGNORECASE,
)


def _ou_subject(text: str) -> str:
    """What an Over/Under line is counting. ``goals`` when it says nothing."""
    for name, pattern in _OU_SUBJECT_PATTERNS:
        if pattern.search(text):
            return name
    return "goals"


TEAM_DELIMITERS = [
    r"\s+vs\.?\s+",
    r"\s+v\s+",
    r"\s+-\s+",
    r"\s+–\s+",
    r"\s+—\s+",
]
COMBINED_TEAM_DELIM = re.compile("|".join(TEAM_DELIMITERS), re.IGNORECASE)


def clean_team_name(name: str) -> str:
    """Strip extraneous symbols, leading numbers, or accidental tokens."""
    name = EMOJI_PATTERN.sub("", name)
    name = LEADING_NUMBER_PATTERN.sub("", name)
    name = TIME_PATTERN.sub("", name)
    name = re.sub(r"[\(\)\[\]\{\}\*\_~]", " ", name)
    name = re.sub(r"\s+", " ", name)
    return name.strip()


def parse_market_and_selection(pred_str: str) -> tuple[MarketCategory, str, Optional[str], Optional[float]]:
    """
    Classify the market category, normalized outcome selection, specifier, and optional odds.
    """
    raw_pred = pred_str.strip()
    
    odds_val = None
    odds_match = ODDS_PATTERN.search(raw_pred)
    if odds_match:
        try:
            odds_val = float(odds_match.group(1))
            raw_pred = ODDS_PATTERN.sub("", raw_pred).strip()
        except ValueError:
            pass

    p_clean = raw_pred.strip().upper()
    p_clean = re.sub(r"^[\s\(\[\{:\-–—\>]+|[\s\)\]\}:]+$", "", p_clean).strip()

    # 0. Corners as a match-winner style market ("most corners").
    if _CORNERS_RE.search(p_clean) and not re.search(r"\b(OVER|UNDER|O|U)[\s/]*[0-9]", p_clean):
        if re.search(r"\b(1|HOME)\b", p_clean):
            return MarketCategory.CORNERS_1X2, "1", None, odds_val
        if re.search(r"\b(2|AWAY)\b", p_clean):
            return MarketCategory.CORNERS_1X2, "2", None, odds_val
        if re.search(r"\b(X|DRAW|TIE)\b", p_clean):
            return MarketCategory.CORNERS_1X2, "X", None, odds_val

    # 1. Over / Under — goals unless the line names another subject.
    #
    # This branch used to match "Over N" and emit `total=N` whatever followed it,
    # so "Over 9.5 Corners" was classified as a GOALS bet and resolved against
    # SportyBet market 18. The line read as corners in the digest and settled as
    # goals on the slip. Silently booking a market nobody asked for is worse
    # than booking nothing, so an unrecognised subject now falls through to
    # UNKNOWN and the leg is reported unmatched instead.
    ou_match = re.search(r"\b(OVER|UNDER|O|U)[\s/]*([0-9]+(?:\.[0-9]+)?)\b", p_clean, re.IGNORECASE)
    if ou_match:
        side_token = ou_match.group(1).upper()
        line_val = ou_match.group(2)
        if "." not in line_val:
            line_val = f"{line_val}.5"
        side = "Over" if side_token in ("OVER", "O") else "Under"
        subject = _ou_subject(p_clean)

        # Anything left once the Over/Under expression and its subject are
        # removed is a QUALIFIER, and a qualifier changes which market this is.
        # "HT Over 0.5" is the first half (market 46), "Early Goals Over 1.5" is
        # market 60180, "Newcastle Over 1.5" is that team's total (market 19) —
        # none of them are full-time goals, but all of them used to be booked as
        # full-time goals because this branch only ever looked at the number.
        # Qualified lines fall through to the description resolver, which reads
        # the market names off the live event instead of guessing.
        residual = _OU_EXPR_RE.sub(" ", p_clean)
        residual = _SUBJECT_WORDS_RE.sub(" ", residual)
        qualified = bool(residual.strip())

        if qualified:
            return MarketCategory.UNKNOWN, p_clean, None, odds_val
        if subject == "goals":
            return MarketCategory.OVER_UNDER, f"{side} {line_val}", f"total={line_val}", odds_val
        if subject == "corners":
            return (MarketCategory.CORNERS, f"{side} {line_val} Corners",
                    f"total={line_val}", odds_val)
        # A subject SportyBet lists but this engine cannot price or grade.
        return MarketCategory.UNKNOWN, p_clean, None, odds_val

    # 2. Both Teams To Score (GG / NG / BTTS)
    if re.search(r"\b(GG|BTTS\s*YES|BOTH\s*TEAMS?\s*TO\s*SCORE\s*YES|BOTH\s*TO\s*SCORE|YES)\b", p_clean, re.IGNORECASE):
        return MarketCategory.BTTS, "GG", None, odds_val
    if re.search(r"\b(NG|BTTS\s*NO|BOTH\s*TEAMS?\s*TO\s*SCORE\s*NO|NO)\b", p_clean, re.IGNORECASE):
        return MarketCategory.BTTS, "NG", None, odds_val

    # 3. Double Chance (1X, 12, X2)
    dc_match = re.search(r"\b(1X|12|X2|1/X|1/2|X/2|HOME/DRAW|HOME/AWAY|DRAW/AWAY)\b", p_clean, re.IGNORECASE)
    if dc_match:
        val = dc_match.group(1).upper().replace("/", "")
        if val in ("1X", "HOME/DRAW", "HOMEDRAW"):
            return MarketCategory.DOUBLE_CHANCE, "1X", None, odds_val
        elif val in ("12", "HOME/AWAY", "HOMEAWAY"):
            return MarketCategory.DOUBLE_CHANCE, "12", None, odds_val
        elif val in ("X2", "2X", "DRAW/AWAY", "DRAWAWAY"):
            return MarketCategory.DOUBLE_CHANCE, "X2", None, odds_val

    # 4. Draw No Bet (DNB)
    dnb_match = re.search(r"\b(?:DNB|DRAW\s*NO\s*BET)\s*(1|2|HOME|AWAY)?\b", p_clean, re.IGNORECASE)
    if dnb_match:
        side = dnb_match.group(1) or "1"
        sel = "1" if side in ("1", "HOME") else "2"
        return MarketCategory.DRAW_NO_BET, f"DNB {sel}", None, odds_val

    # 5. 1X2 / Match Winner
    if p_clean in ("1", "HOME", "HOME WIN", "HOMEWIN", "W1", "1 (HOME)"):
        return MarketCategory.MATCH_WINNER, "1", None, odds_val
    if p_clean in ("X", "DRAW", "TIE", "X (DRAW)"):
        return MarketCategory.MATCH_WINNER, "X", None, odds_val
    if p_clean in ("2", "AWAY", "AWAY WIN", "AWAYWIN", "W2", "2 (AWAY)"):
        return MarketCategory.MATCH_WINNER, "2", None, odds_val

    if re.search(r"\b(HOME\s*WIN|W1|1\s*WIN)\b", p_clean, re.IGNORECASE):
        return MarketCategory.MATCH_WINNER, "1", None, odds_val
    if re.search(r"\b(AWAY\s*WIN|W2|2\s*WIN)\b", p_clean, re.IGNORECASE):
        return MarketCategory.MATCH_WINNER, "2", None, odds_val
    if re.search(r"\b(DRAW|FULL\s*TIME\s*DRAW)\b", p_clean, re.IGNORECASE):
        return MarketCategory.MATCH_WINNER, "X", None, odds_val

    return MarketCategory.UNKNOWN, p_clean, None, odds_val


def parse_prediction_line(line: str) -> Optional[ParsedBet]:
    """
    Parse a single raw line into a ParsedBet object.
    Returns None if no valid fixture structure could be recognized.
    """
    raw_line = line.strip()
    if not raw_line or len(raw_line) < 5:
        return None

    if any(ignore_kw in raw_line.lower() for ignore_kw in [
        "join channel", "vip group", "booking code", "total odds", "sure odds", "telegram.me", "t.me/", "http://"
    ]):
        return None

    cleaned_line = LEADING_NUMBER_PATTERN.sub("", raw_line).strip()
    cleaned_line = EMOJI_PATTERN.sub(" ", cleaned_line).strip()

    match_part = ""
    pred_part = ""

    # Check parentheses first e.g. "PSG vs Marseille (Over 3.5)"
    parentheses_match = re.search(r"^(.*?)\s*\(([^)]+)\)\s*(?:@\s*\d+\.\d+)?$", cleaned_line)
    if parentheses_match:
        cand_match = parentheses_match.group(1).strip()
        cand_pred = parentheses_match.group(2).strip()
        if any(d in cand_match.lower() for d in [" vs", " v ", " - ", " – "]):
            match_part = cand_match
            pred_part = cand_pred

    if not match_part:
        # Strong prediction delimiters (colon, arrow, pipe, tab)
        # The colon must not be one inside a scoreline or a handicap. Splitting
        # on any ":" cut "Handicap 0:1 Home" at the 0, leaving the away team as
        # "Bournemouth - Handicap 0" and the market as "1 Home" — a different
        # bet on a different fixture. A colon between two digits is part of the
        # market, never the separator before it.
        strong_split = re.split(
            r"(?:\s*=>\s*|\s*->\s*|\s*\|\s*|(?<!\d)\s*:\s*(?!\d)|\t+)",
            cleaned_line, maxsplit=1,
        )
        if len(strong_split) == 2:
            match_part = strong_split[0].strip()
            pred_part = strong_split[1].strip()
        else:
            # "Home vs Away - <anything>": the fixture ends at the first " - "
            # that follows the vs, and everything after it is the market.
            #
            # This case used to fall to the keyword split below, which scans for
            # a market keyword with a lazy prefix and so cut the line at the
            # FIRST keyword-looking token anywhere in it. On
            # "Ajax vs PSV - Correct Score 2-1" the `\b[1X2]\b` alternative
            # matched the "2" of the scoreline, leaving the fixture as
            # "Ajax vs PSV - Correct Score", which then split into three parts
            # on the team delimiters and the whole line was discarded. Same for
            # "Inter vs Milan - HT Over 0.5" and "Roma vs Lazio - Home -1.5":
            # any market text containing a hyphen or a bare 1/X/2 was dropped
            # silently rather than booked.
            vs_and_dash = re.match(
                r"^(?P<fixture>.+?\s+(?:vs\.?|v)\s+.+?)\s+[-–—]\s+(?P<market>\S.*)$",
                cleaned_line, re.IGNORECASE,
            )
            kw_match = None
            if vs_and_dash:
                match_part = vs_and_dash.group("fixture").strip()
                pred_part = vs_and_dash.group("market").strip()
            else:
                # Check for keyword split (e.g. "Arsenal vs Chelsea Over 2.5")
                kw_match = re.search(r"^(.*?)\s+(?:[-–—]\s*)?(OVER\s*[\d\.]+|UNDER\s*[\d\.]+|GG|NG|BTTS[^\s]*|1X|X2|12|DNB\s*[12]?|HOME\s*WIN|AWAY\s*WIN|DRAW|\b[1X2]\b)(.*)$", cleaned_line, re.IGNORECASE)
            if kw_match:
                match_part = kw_match.group(1).strip()
                pred_part = (kw_match.group(2) + " " + kw_match.group(3)).strip()
            elif not match_part:
                # Last resort: split on last hyphen
                hyphen_split = re.split(r"\s+[-–—]\s+", cleaned_line)
                if len(hyphen_split) >= 3:
                    match_part = f"{hyphen_split[0]} vs {hyphen_split[1]}"
                    pred_part = " - ".join(hyphen_split[2:])
                elif len(hyphen_split) == 2:
                    match_part = hyphen_split[0]
                    pred_part = hyphen_split[1]
                else:
                    return None

    # Split Home and Away team. "vs"/"v" is unambiguous, so when it is present
    # it decides the split; the dash forms are only a fallback, because a dash
    # is also how a market is separated from its fixture.
    vs_split = re.split(r"\s+(?:vs\.?|v)\s+", match_part, maxsplit=1, flags=re.IGNORECASE)
    if len(vs_split) == 2:
        team_split = vs_split
    else:
        team_split = COMBINED_TEAM_DELIM.split(match_part)
    if len(team_split) != 2:
        return None

    home_team = clean_team_name(team_split[0])
    away_team = clean_team_name(team_split[1])

    if not home_team or not away_team or len(home_team) < 2 or len(away_team) < 2:
        return None

    market_cat, selection, specifier, odds = parse_market_and_selection(pred_part)

    return ParsedBet(
        raw_line=raw_line,
        home_team=home_team,
        away_team=away_team,
        market_category=market_cat,
        selection=selection,
        specifier=specifier,
        odds=odds,
        confidence=0.9 if market_cat != MarketCategory.UNKNOWN else 0.5,
        raw_selection=pred_part.strip() or None,
    )


def unparsed_lines(text: str) -> list[str]:
    """
    Lines that carry content but produced no bet.

    A line this parser cannot read is not reported anywhere downstream — it
    simply never becomes a ``ParsedBet``, so a slip of ten games could come
    back as eight with nothing said about the other two. Callers use this to
    tell the user which of their lines were never considered.
    """
    rejected: list[str] = []
    for line in (text or "").split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if parse_prediction_line(stripped) is None:
            rejected.append(stripped)
    return rejected


def parse_prediction_text(text: str) -> list[ParsedBet]:
    """
    Parse a multiline prediction text block and return all detected ParsedBet items.
    """
    lines = text.strip().split("\n")
    results: list[ParsedBet] = []

    for line in lines:
        line_str = line.strip()
        if not line_str:
            continue
        parsed = parse_prediction_line(line_str)
        if parsed:
            results.append(parsed)

    return results
