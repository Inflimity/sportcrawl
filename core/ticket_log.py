"""
Append-only record of every leg this engine books, with its price.

Why this exists
---------------
SportyBet publishes no historical odds. Once a day passes, what a leg cost is
unrecoverable — which is why "does Ticket 4 beat its price?" could never be
answered from stored data, only from whatever survived in the Telegram chat.

Outcomes are recoverable (SofaScore keeps team histories, and
``tools.backtest_markets.outcomes_from_events`` reads finished scores straight
out of them). **Price is the perishable half.** This module captures it at the
moment of booking so that grading later needs nothing but the log and a fetch.

Design rules
------------
- **It can never break a digest.** Every public call swallows its own errors.
  A prediction run that fails because a log file was unwritable would be a
  self-inflicted outage, and the log is worth less than the ticket.
- **One row per leg, not per ticket.** Ticket-level grading over a month gives
  ~30 data points; leg-level gives ~60 and they are near-independent, so the
  leg rate pins the ticket rate far more tightly for the same data.
- **Raw values only.** No derived probabilities, no verdicts. Anything computed
  here would freeze today's opinion into the record; the analysis belongs in
  the tool that reads it.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_LOG_PATH = os.getenv("TICKET_LOG_PATH", "logs/tickets.jsonl")


def _leg_rows(
    ticket_name: str,
    ticket: Any,
    booking_result: Any = None,
    extra: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Flatten one ticket into a row per leg. Never raises on a missing field."""
    legs = getattr(ticket, "legs", None) or []
    odds = list(getattr(ticket, "leg_odds", None) or [])
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    code = getattr(booking_result, "booking_code", None)
    booked = bool(getattr(booking_result, "success", False) and code)

    combined = getattr(ticket, "combined_odds", None)
    joint = getattr(ticket, "combined_probability", None)

    rows: list[dict[str, Any]] = []
    for idx, leg in enumerate(legs):
        fx = getattr(leg, "fixture", None)
        price = odds[idx] if idx < len(odds) else None
        rows.append({
            "logged_at": stamp,
            "ticket": ticket_name,
            "booking_code": code,
            "booked": booked,
            "ticket_legs": len(legs),
            "ticket_odds": round(combined, 4) if isinstance(combined, (int, float)) else None,
            "ticket_probability": round(joint, 4) if isinstance(joint, (int, float)) else None,
            "leg_index": idx + 1,
            # Identity, so the outcome can be found later without guessing at names.
            "match_id": getattr(fx, "match_id", None),
            "home_id": getattr(fx, "home_id", None),
            "away_id": getattr(fx, "away_id", None),
            "home_name": getattr(fx, "home_name", None),
            "away_name": getattr(fx, "away_name", None),
            "tournament": getattr(fx, "tournament", None),
            "kickoff_utc": getattr(fx, "start_utc", None),
            # The bet itself.
            "selection": getattr(leg, "selection", None),
            "market": getattr(leg, "market", None),
            "model_probability": (round(p, 4) if isinstance(
                (p := getattr(leg, "probability", None)), (int, float)) else None),
            # The perishable half. Without this the row is worth little.
            "price": round(price, 4) if isinstance(price, (int, float)) else None,
            "rationale": getattr(leg, "rationale", None),
            **(extra or {}),
        })
    return rows


def log_ticket(
    ticket_name: str,
    ticket: Any,
    booking_result: Any = None,
    path: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> int:
    """
    Append one row per leg. Returns rows written; 0 on any failure.

    Deliberately total: a logging fault must never surface as a failed
    prediction, so everything here is caught and reported as a warning.
    """
    try:
        if ticket is None or not getattr(ticket, "legs", None):
            return 0
        rows = _leg_rows(ticket_name, ticket, booking_result, extra)
        if not rows:
            return 0
        target = Path(path or DEFAULT_LOG_PATH)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return len(rows)
    except Exception as e:  # noqa: BLE001 - see docstring
        logger.warning("Ticket log write failed (%s); continuing without it.", e)
        return 0


def log_result(result: Any, path: Optional[str] = None) -> int:
    """Log every ticket on a DualPipelineResult. Returns total rows written.""" 
    written = 0
    try:
        for name, holder in (("top_10", getattr(result, "tier_10", None)),
                             ("top_20", getattr(result, "tier_20", None))):
            picks = getattr(holder, "picks", None)
            if picks:
                # Tickets 1 and 2 carry picks rather than a Ticket object.
                shim = type("_T", (), {"legs": picks, "leg_odds": [
                    getattr(p, "odds", None) for p in picks]})()
                written += log_ticket(name, shim, getattr(holder, "booking_result", None), path)

        two = getattr(result, "two_odds", None)
        if two is not None:
            written += log_ticket("two_odds", getattr(two, "ticket", None),
                                  getattr(two, "booking_result", None), path)

        draws = getattr(result, "draws", None)
        for entry in (getattr(draws, "tickets", None) or []):
            tk = getattr(entry, "ticket", None)
            written += log_ticket(f"draw_{getattr(tk, 'label', 'ticket')}", tk,
                                  getattr(entry, "booking_result", None), path)
    except Exception as e:  # noqa: BLE001
        logger.warning("Ticket log failed (%s); continuing without it.", e)
    return written
