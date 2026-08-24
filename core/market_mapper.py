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
            "GG": {"id": "74", "desc": "Yes"},
            "YES": {"id": "74", "desc": "Yes"},
            "NG": {"id": "75", "desc": "No"},
            "NO": {"id": "75", "desc": "No"},
        },
    },
    MarketCategory.DRAW_NO_BET: {
        "market_id": "11",
        "name": "Draw No Bet",
        "outcomes": {
            "DNB 1": {"id": "1", "desc": "Home"},
            "1": {"id": "1", "desc": "Home"},
            "DNB 2": {"id": "2", "desc": "Away"},
            "2": {"id": "2", "desc": "Away"},
        },
    },
}


def resolve_market_selection(
    bet: ParsedBet,
    event_markets: Optional[list[dict[str, Any]]] = None,
) -> Optional[dict[str, Any]]:
    """
    Find matching market & outcome for a parsed bet from SportyBet event data.
    Returns dictionary with {marketId, outcomeId, specifier, odds, desc} or None.
    """
    if not event_markets:
        # Fallback to static mapping
        return _static_resolve(bet)

    # 1. Match Winner (1X2)
    if bet.market_category == MarketCategory.MATCH_WINNER:
        m1 = next((m for m in event_markets if str(m.get("id")) == "1"), None)
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

    # 2. Over / Under
    elif bet.market_category == MarketCategory.OVER_UNDER:
        # Extract target line e.g. 2.5, 1.5, 3.5
        line_match = re.search(r"(\d+(?:\.\d+)?)", bet.selection)
        target_line = line_match.group(1) if line_match else "2.5"
        is_over = "OVER" in bet.selection.upper()

        ou_markets = [m for m in event_markets if str(m.get("id")) == "18"]
        for m in ou_markets:
            spec = m.get("specifier", "") or ""
            if f"total={target_line}" in spec or target_line in spec:
                target_desc = "Over" if is_over else "Under"
                for out in m.get("outcomes", []):
                    if target_desc.lower() in out.get("desc", "").lower() or (is_over and str(out.get("id")) == "12") or (not is_over and str(out.get("id")) == "13"):
                        return {
                            "marketId": "18",
                            "outcomeId": str(out.get("id")),
                            "specifier": spec,
                            "odds": str(out.get("odds", "")),
                            "marketDesc": f"Over/Under {target_line}",
                            "outcomeDesc": out.get("desc", f"{'Over' if is_over else 'Under'} {target_line}"),
                        }

    # 3. Both Teams to Score (GG / NG)
    elif bet.market_category == MarketCategory.BTTS:
        m29 = next((m for m in event_markets if str(m.get("id")) == "29"), None)
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
        m10 = next((m for m in event_markets if str(m.get("id")) == "10"), None)
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

    # 5. Draw No Bet (DNB)
    elif bet.market_category == MarketCategory.DRAW_NO_BET:
        m11 = next((m for m in event_markets if str(m.get("id")) == "11"), None)
        if m11 and m11.get("outcomes"):
            is_home = "1" in bet.selection or "HOME" in bet.selection.upper()
            target_id = "1" if is_home else "2"
            for out in m11.get("outcomes", []):
                if str(out.get("id")) == target_id:
                    return {
                        "marketId": "11",
                        "outcomeId": str(out.get("id")),
                        "specifier": m11.get("specifier", ""),
                        "odds": str(out.get("odds", "")),
                        "marketDesc": "Draw No Bet",
                        "outcomeDesc": out.get("desc", f"DNB {'1' if is_home else '2'}"),
                    }

    # Fallback to static mapping if dynamic didn't match
    return _static_resolve(bet)


def _static_resolve(bet: ParsedBet) -> Optional[dict[str, Any]]:
    """Resolve based on default SportyBet market IDs when event data is not available."""
    config = SPORTYBET_MARKETS.get(bet.market_category)
    if not config:
        return None

    market_id = config["market_id"]
    specifier = bet.specifier or ""

    if bet.market_category == MarketCategory.OVER_UNDER:
        is_over = "OVER" in bet.selection.upper()
        out_id = "12" if is_over else "13"
        return {
            "marketId": market_id,
            "outcomeId": out_id,
            "specifier": specifier or "total=2.5",
            "odds": str(bet.odds or "1.50"),
            "marketDesc": f"Over/Under {specifier or '2.5'}",
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
