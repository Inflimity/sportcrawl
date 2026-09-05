"""
Market Mapper for SportCrawl.

Maps normalized prediction markets and selections into SportyBet market IDs,
specifiers, and outcome IDs.
"""

from __future__ import annotations

import re
from typing import Any, Optional
from core.prediction_parser import MarketCategory, ParsedBet


# Known static SportyBet market definitions
SPORTYBET_MARKETS = {
    MarketCategory.MATCH_WINNER: {
        "market_id": "1",
        "name": "1X2",
        "outcomes": {
            "1": {"id": "1", "desc": "Home"},
            "X": {"id": "2", "desc": "Draw"},
            "2": {"id": "3", "desc": "Away"},
        },
    },
    MarketCategory.DOUBLE_CHANCE: {
        "market_id": "10",
        "name": "Double Chance",
        "outcomes": {
            "1X": {"id": "9", "desc": "Home/Draw"},
            "12": {"id": "10", "desc": "Home/Away"},
            "X2": {"id": "11", "desc": "Draw/Away"},
        },
    },
    MarketCategory.OVER_UNDER: {
        "market_id": "18",
        "name": "Over/Under",
        "outcomes": {
            "OVER": {"id": "12", "desc": "Over"},
            "UNDER": {"id": "13", "desc": "Under"},
        },
    },
    MarketCategory.BTTS: {
        "market_id": "29",
        "name": "Both Teams to Score",
        "outcomes": {
            # Verified against a live event payload: market 29 "GG/NG" carries
            # Yes=74 and No=76. "No" was recorded here as 75, an id that market
            # does not have.
            "GG": {"id": "74", "desc": "Yes"},
            "YES": {"id": "74", "desc": "Yes"},
            "NG": {"id": "76", "desc": "No"},
            "NO": {"id": "76", "desc": "No"},
        },
    },
    MarketCategory.DRAW_NO_BET: {
        "market_id": "11",
        "name": "Draw No Bet",
        "outcomes": {
            # Likewise: market 11 "Draw No Bet" is Home=4 / Away=5, not 1 / 2.
            # 1 and 2 are the 1X2 outcome ids and mean nothing on market 11.
            "DNB 1": {"id": "4", "desc": "Home"},
            "1": {"id": "4", "desc": "Home"},
            "DNB 2": {"id": "5", "desc": "Away"},
            "2": {"id": "5", "desc": "Away"},
        },
    },
    MarketCategory.CORNERS: {
        "market_id": "166",
        "name": "Corners - Over/Under",
        "outcomes": {
            "OVER": {"id": "12", "desc": "Over"},
            "UNDER": {"id": "13", "desc": "Under"},
        },
    },
    MarketCategory.CORNERS_1X2: {
        "market_id": "162",
        "name": "Corners - 1X2",
        "outcomes": {
            "1": {"id": "1", "desc": "Home"},
            "X": {"id": "2", "desc": "Draw"},
            "2": {"id": "3", "desc": "Away"},
        },
    },
}


# Over/Under-shaped markets: category -> (SportyBet market id, human name).
# Both use outcome 12 for Over and 13 for Under; only the market id differs,
# which is exactly why a corners line misrouted to market 18 settled as goals.
_TOTALS_MARKETS = {
    MarketCategory.OVER_UNDER: ("18", "Over/Under"),
    MarketCategory.CORNERS: ("166", "Corners - Over/Under"),
}


# SportyBet lists most markets twice on an upcoming event: a prematch copy
# (product 3) and a live copy (product 1) that stays suspended until kickoff.
# Measured over four events, 3,021 entries were product 3 / status 0 against
# 1,028 product 1 / status 1, and the two copies carry different prices —
# 1X2 Home at 2.17 on one and 2.15 on the other. Taking whichever came first
# in the list picked a suspended copy 4 times in 944, and made the logged price
# depend on list order.
_MARKET_OPEN = 0


def _iter_markets(event_markets, market_id):
    """Every copy of one market, open ones first."""
    same = [m for m in event_markets if str(m.get("id")) == str(market_id)]
    return sorted(same, key=lambda m: 0 if m.get("status") == _MARKET_OPEN else 1)


def _find_market(event_markets, market_id):
    """The open copy of a market if there is one, else any copy, else None."""
    found = _iter_markets(event_markets, market_id)
    return found[0] if found else None


def resolve_market_selection(
    bet: ParsedBet,
    event_markets: Optional[list[dict[str, Any]]] = None,
) -> Optional[dict[str, Any]]:
    """
    Find matching market & outcome for a parsed bet from SportyBet event data.
    Returns dictionary with {marketId, outcomeId, specifier, odds, desc} or None.
    """
    if not event_markets:
        # No event data at all: the static table is the only thing left. Its
        # ids are correct for a normal event, and the caller is responsible for
        # whether booking blind is acceptable.
        return _static_resolve(bet)

    # 1. Match Winner (1X2)
    if bet.market_category == MarketCategory.MATCH_WINNER:
        m1 = _find_market(event_markets, "1")
        if m1 and m1.get("outcomes"):
            target_desc = {"1": "Home", "X": "Draw", "2": "Away"}.get(bet.selection.upper(), "")
            for out in m1.get("outcomes", []):
                if out.get("desc", "").upper() == target_desc.upper() or (bet.selection == "1" and out.get("id") == "1") or (bet.selection == "X" and out.get("id") == "2") or (bet.selection == "2" and out.get("id") == "3"):
                    return {
                        "marketId": "1",
                        "outcomeId": str(out.get("id")),
                        "specifier": m1.get("specifier", ""),
                        "odds": str(out.get("odds", "")),
                        "marketDesc": "1X2",
                        "outcomeDesc": out.get("desc", bet.selection),
                    }

    # 2. Over / Under totals — goals (market 18) and corners (market 166).
    #    Same outcome ids, different market id: routing a corners line to 18
    #    books goals under a corners label.
    elif bet.market_category in _TOTALS_MARKETS:
        market_id, market_name = _TOTALS_MARKETS[bet.market_category]
        # Extract target line e.g. 2.5, 1.5, 3.5
        line_match = re.search(r"(\d+(?:\.\d+)?)", bet.selection)
        target_line = line_match.group(1) if line_match else "2.5"
        is_over = "OVER" in bet.selection.upper()

        ou_markets = _iter_markets(event_markets, market_id)
        for m in ou_markets:
            spec = m.get("specifier", "") or ""
            # Exact specifier only. `target_line in spec` also matched
            # "total=1.5" for a 1.5 line inside "total=11.5", and matched
            # compound specifiers such as "minute=15|total=0.5".
            if spec != f"total={target_line}":
                continue
            target_desc = "Over" if is_over else "Under"
            for out in m.get("outcomes", []):
                if target_desc.lower() in out.get("desc", "").lower() or (is_over and str(out.get("id")) == "12") or (not is_over and str(out.get("id")) == "13"):
                    return {
                        "marketId": market_id,
                        "outcomeId": str(out.get("id")),
                        "specifier": spec,
                        "odds": str(out.get("odds", "")),
                        "marketDesc": f"{market_name} {target_line}",
                        "outcomeDesc": out.get("desc", f"{'Over' if is_over else 'Under'} {target_line}"),
                    }

    # 3. Both Teams to Score (GG / NG)
    elif bet.market_category == MarketCategory.BTTS:
        m29 = _find_market(event_markets, "29")
        if m29 and m29.get("outcomes"):
            is_yes = bet.selection.upper() in ("GG", "YES")
            target_desc = "Yes" if is_yes else "No"
            for out in m29.get("outcomes", []):
                if target_desc.lower() in out.get("desc", "").lower() or (is_yes and str(out.get("id")) == "74") or (not is_yes and str(out.get("id")) == "75"):
                    return {
                        "marketId": "29",
                        "outcomeId": str(out.get("id")),
                        "specifier": m29.get("specifier", ""),
                        "odds": str(out.get("odds", "")),
                        "marketDesc": "Both Teams to Score",
                        "outcomeDesc": "GG (Yes)" if is_yes else "NG (No)",
                    }

    # 4. Double Chance (1X, 12, X2)
    elif bet.market_category == MarketCategory.DOUBLE_CHANCE:
        m10 = _find_market(event_markets, "10")
        if m10 and m10.get("outcomes"):
            target_key = bet.selection.upper()
            target_out_id = {"1X": "9", "12": "10", "X2": "11"}.get(target_key, "9")
            for out in m10.get("outcomes", []):
                if str(out.get("id")) == target_out_id or target_key in out.get("desc", "").upper().replace("/", ""):
                    return {
                        "marketId": "10",
                        "outcomeId": str(out.get("id")),
                        "specifier": m10.get("specifier", ""),
                        "odds": str(out.get("odds", "")),
                        "marketDesc": "Double Chance",
                        "outcomeDesc": out.get("desc", target_key),
                    }

    # 4b. Corners 1X2 — which side wins the corner count.
    elif bet.market_category == MarketCategory.CORNERS_1X2:
        m162 = _find_market(event_markets, "162")
        if m162 and m162.get("outcomes"):
            target_desc = {"1": "Home", "X": "Draw", "2": "Away"}.get(bet.selection.upper(), "")
            for out in m162.get("outcomes", []):
                if out.get("desc", "").upper() == target_desc.upper():
                    return {
                        "marketId": "162",
                        "outcomeId": str(out.get("id")),
                        "specifier": m162.get("specifier", ""),
                        "odds": str(out.get("odds", "")),
                        "marketDesc": "Corners - 1X2",
                        "outcomeDesc": out.get("desc", bet.selection),
                    }

    # 5. Draw No Bet (DNB)
    elif bet.market_category == MarketCategory.DRAW_NO_BET:
        m11 = _find_market(event_markets, "11")
        if m11 and m11.get("outcomes"):
            is_home = "1" in bet.selection or "HOME" in bet.selection.upper()
            # Market 11 outcomes are Home=4 / Away=5. This matched on "1"/"2",
            # which are 1X2's ids and appear on no Draw No Bet market, so every
            # DNB leg fell through unresolved.
            target_desc = "Home" if is_home else "Away"
            for out in m11.get("outcomes", []):
                if out.get("desc", "").strip().upper() == target_desc.upper():
                    return {
                        "marketId": "11",
                        "outcomeId": str(out.get("id")),
                        "specifier": m11.get("specifier", ""),
                        "odds": str(out.get("odds", "")),
                        "marketDesc": "Draw No Bet",
                        "outcomeDesc": out.get("desc", f"DNB {'1' if is_home else '2'}"),
                    }

    # Free-text fallback, and ONLY when no fast path claimed this line.
    #
    # The gate matters. "Over 9.5" is classified as goals, and this event has no
    # 9.5 goals line — but it does have "Corners - Over/Under" at total=9.5, and
    # an ungated description match happily returned it. That is the same silent
    # market swap the corners work was meant to end, arriving through the back
    # door. When a category is known, a failed lookup means the market is not
    # offered here; it never means "take a different market with similar words".
    if bet.market_category == MarketCategory.UNKNOWN:
        by_text = resolve_by_description(bet.raw_selection or bet.selection, event_markets)
        if by_text:
            return by_text

    # No static fallback here. `event_markets` was supplied and searched, so a
    # miss means this market is genuinely not offered on this event — many
    # lower-league events carry only a handful of markets, and one carried a
    # single 1X2. Falling through to the static table invented a selection on a
    # market the event does not have, at a hardcoded 1.50 that was multiplied
    # into the ticket's total odds and written to the price log. Returning None
    # reports the leg as unmatched, which is the truth.
    return None


def _static_resolve(bet: ParsedBet) -> Optional[dict[str, Any]]:
    """Resolve based on default SportyBet market IDs when event data is not available."""
    config = SPORTYBET_MARKETS.get(bet.market_category)
    if not config:
        return None

    market_id = config["market_id"]
    specifier = bet.specifier or ""

    if bet.market_category in _TOTALS_MARKETS:
        # Corners share the Over/Under shape — outcome 12 Over, 13 Under — and
        # would otherwise fall through to the outcome-key lookup below, where
        # "OVER 9.5 CORNERS" matches nothing and the leg is silently dropped.
        _, market_name = _TOTALS_MARKETS[bet.market_category]
        is_over = "OVER" in bet.selection.upper()
        out_id = "12" if is_over else "13"
        return {
            "marketId": market_id,
            "outcomeId": out_id,
            "specifier": specifier or "total=2.5",
            "odds": str(bet.odds or "1.50"),
            "marketDesc": f"{market_name} {specifier or '2.5'}",
            "outcomeDesc": bet.selection,
        }

    outcomes = config.get("outcomes", {})
    key = bet.selection.upper()
    if key in outcomes:
        target = outcomes[key]
        return {
            "marketId": market_id,
            "outcomeId": target["id"],
            "specifier": specifier,
            "odds": str(bet.odds or "1.50"),
            "marketDesc": config.get("name", ""),
            "outcomeDesc": target.get("desc", bet.selection),
        }

    return None


# ── Resolving free text against the live market catalogue ──────────────────
#
# A SportyBet event carries ~1,000 markets. Hardcoding a branch per market is
# hopeless and always behind, but every market already arrives with everything
# needed to identify it: a `desc` ("Over/Under - Early Goals"), a `specifier`
# ("minsnr=10|total=1.5") and an outcome `desc` ("Over 1.5"). So anything the
# fast paths above do not classify is matched against those strings instead.
#
# The rule is deliberately strict, because booking a market the user did not
# ask for is worse than booking nothing:
#
#   1. Every token the user wrote must appear in the candidate's own text.
#      "Early Goals Over 1.5" cannot match plain "Over/Under Over 1.5",
#      because nothing there accounts for "early".
#   2. Among the candidates that explain everything, the one carrying the
#      fewest *unexplained extra* tokens wins — the most specific reading.
#   3. A tie at the top is refused, not guessed. "Home -1.5" matches Asian
#      Handicap, Corner Handicap and Bookings Handicap equally well, and the
#      honest answer is to ask for a more specific line.

_SCORE_RE = re.compile(r"^(\d{1,2})[-:](\d{1,2})$")
# "1/1", "2/2", "X/X", "X/1", "2/X" can only be Half Time/Full Time.
# "1/X", "1/2" and "X/2" are how most people write DOUBLE CHANCE, and the
# double-chance fast path in the parser owns them; write "HT/FT 1/X" for the
# half-time reading. Guessing between the two would book the wrong market on
# the most commonly written lines there are.
_HT_FT_RE = re.compile(r"^(?:1\s*/\s*1|2\s*/\s*2|X\s*/\s*X|X\s*/\s*1|2\s*/\s*X)$", re.IGNORECASE)
_HT_FT_SIDES_RE = re.compile(r"^([12X])\s*/\s*([12X])$", re.IGNORECASE)
_SIDE_WORDS = {"1": "home", "X": "draw", "2": "away"}

# Shorthand a person writes, rewritten into the wording SportyBet uses.
# Applied to the whole string before tokenizing, longest forms first.
_TEXT_REWRITES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rf"\b{pattern}\b", re.IGNORECASE), replacement)
    for pattern, replacement in (
        (r"half\s*time\s*/\s*full\s*time", "half time full time"),
        (r"ht\s*/\s*ft", "half time full time"),
        (r"htft", "half time full time"),
        (r"first\s*half", "1st half"),
        (r"second\s*half", "2nd half"),
        (r"ht", "1st half"),
        (r"1h", "1st half"),
        (r"2h", "2nd half"),
        (r"ft", "full time"),
        (r"ah", "asian handicap"),
        (r"cs", "correct score"),
        (r"o\s*/\s*u", "over under"),
        (r"cards?", "bookings"),
    )
)


def _tokenize(text: str) -> list[str]:
    """Comparable tokens: words, and numbers keeping sign, decimal and colon."""
    if not text:
        return []
    lowered = text.lower().replace("(", " ").replace(")", " ")
    raw = re.findall(r"[a-z]+\d*|[-+]?\d+(?:\.\d+)?(?::\d+)?", lowered)
    tokens: list[str] = []
    for tok in raw:
        # "-1.5" and "+1.5" name the same handicap line from either side.
        if tok.startswith("+"):
            tok = tok[1:]
        tokens.append(tok)
    return tokens


def _query_tokens(text: str) -> list[str]:
    """Tokens of what the user wrote, with shorthand expanded."""
    stripped = (text or "").strip()

    if _HT_FT_RE.match(stripped):
        sides = _HT_FT_SIDES_RE.match(stripped)
        first = _SIDE_WORDS[sides.group(1).upper()]
        second = _SIDE_WORDS[sides.group(2).upper()]
        return ["half", "time", "full", "time", first, second]

    # Rewrite the string, then tokenize once. Expanding shorthand into
    # already-split tokens does not work: "ht" expands to "1st half", and
    # "1st" tokenizes as ["1", "st"] on the market-description side, so the
    # two sides never met.
    lowered = stripped.lower()
    for short, long in _TEXT_REWRITES:
        lowered = short.sub(long, lowered)
    # Correct-score lines are written "2-1" and listed "2:1".
    lowered = re.sub(r"\b(\d{1,2})-(\d{1,2})\b", r"\1:\2", lowered)
    # Once a line has said Half Time/Full Time, "1/X" means Home/Draw. Outside
    # that context the same characters are double chance, which the parser's
    # fast path has already claimed, so this rewrite stays scoped to HT/FT.
    if "half time full time" in lowered:
        lowered = re.sub(
            r"\b([12x])\s*/\s*([12x])\b",
            lambda m: f"{_SIDE_WORDS[m.group(1).upper()]} {_SIDE_WORDS[m.group(2).upper()]}",
            lowered,
        )
    return _tokenize(lowered)


def _candidate_tokens(market: dict[str, Any], outcome: dict[str, Any]) -> list[str]:
    """Everything that identifies one bookable selection, as tokens."""
    tokens = _tokenize(market.get("desc") or "")
    # Specifier values only: "minsnr=10|total=1.5" identifies the market by its
    # 10 and its 1.5, never by the key names.
    for part in (market.get("specifier") or "").split("|"):
        _, _, value = part.partition("=")
        tokens.extend(_tokenize(value or part))
    tokens.extend(_tokenize(outcome.get("desc") or ""))
    return tokens


def resolve_by_description(
    text: str,
    event_markets: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """
    Match free-text market wording against the markets this event actually has.

    Returns the same shape as ``resolve_market_selection``, or ``None`` when
    nothing explains the text or more than one thing explains it equally well.
    """
    wanted = _query_tokens(text)
    if not wanted or not event_markets:
        return None

    scored: list[tuple[int, int, int, dict[str, Any]]] = []
    seen: set[tuple[str, str, str]] = set()

    for market in sorted(event_markets, key=lambda m: 0 if m.get("status") == _MARKET_OPEN else 1):
        for outcome in market.get("outcomes") or []:
            key = (str(market.get("id")), market.get("specifier") or "", str(outcome.get("id")))
            if key in seen:
                continue
            seen.add(key)
            if outcome.get("isActive") == 0:
                continue

            pool = _candidate_tokens(market, outcome)
            explained = 0
            for token in wanted:
                if token in pool:
                    pool.remove(token)
                    explained += 1
            if explained != len(wanted):
                continue

            # How much of the OUTCOME's own name the query failed to account
            # for. This is the discriminating measure: "Odd/Even" repeats both
            # outcome names in its market description, so a query of "odd"
            # leaves the same number of leftover tokens whether it is paired
            # with the Odd outcome or the Even one, and ranking on the total
            # alone made every Odd/Even line an unresolvable tie.
            outcome_left = [
                t for t in _tokenize(outcome.get("desc") or "") if t not in wanted
            ]

            suspended = 0 if market.get("status") == _MARKET_OPEN else 1
            scored.append((suspended, len(outcome_left), len(pool), {
                "marketId": str(market.get("id")),
                "outcomeId": str(outcome.get("id")),
                "specifier": market.get("specifier", "") or "",
                "odds": str(outcome.get("odds", "")),
                "marketDesc": market.get("desc", ""),
                "outcomeDesc": outcome.get("desc", ""),
            }))

    if not scored:
        return None

    scored.sort(key=lambda item: (item[0], item[1], item[2]))
    best = scored[0]
    if len(scored) > 1 and scored[1][:3] == best[:3]:
        # Equally good readings of the same words. Refuse rather than pick.
        return None
    return best[3]
