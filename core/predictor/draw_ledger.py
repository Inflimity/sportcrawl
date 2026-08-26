"""
Append-only record of every draw pick the engine has ever emitted.

The draw track needs a sample before anything it produces can be believed, and
that sample has to start accumulating from the first run — retro-fitting one
later means re-fetching form under SofaScore's rate limiting, which is the
thing that got the local IP blocked in the first place.

How large a sample
------------------
At a true 30% hit rate the standard error on 142 picks is 3.8 points, so 142
picks cannot distinguish a 30% strategy from a 38% one. Reading ROI needs
400-500 graded picks at minimum and closer to 1,000 to be comfortable. At ten
picks a day that is six to sixteen weeks.

Closing line value gets there faster. Re-fetch each pick's draw price shortly
before kickoff and store it as ``closing_odds``: beating the closing line is
readable at around 150 picks, where hit rate is still pure noise. It is the
only signal available on a useful timescale, which makes ``closing_odds`` the
most valuable column in this file.

Kept separate from the main engine's results on purpose — the 142-pick backtest
tuned floors for goals and double-chance markets, and mixing draw outcomes into
it would corrupt a calibration that is currently sound.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from core.predictor.odds import PricedPick

logger = logging.getLogger("SportCrawl.Predictor.DrawLedger")

DEFAULT_LEDGER_PATH = Path("storage/draw_ledger.jsonl")


def record_picks(
    priced: Iterable[PricedPick],
    ticket_label: Optional[str] = None,
    path: Path = DEFAULT_LEDGER_PATH,
) -> int:
    """
    Append one row per draw pick. Returns the number of rows written.

    Every pick is recorded, priced or not. An unpriced pick is a fact about
    market availability worth keeping — a strategy that generates selections
    SportyBet will not price is not a strategy.
    """
    rows: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc).isoformat()

    for item in priced:
        pick = item.pick
        rows.append(
            {
                "recorded_at": now,
                "kickoff": getattr(pick.fixture, "start_local", None),
                # Unix seconds, so the sweep can tell what is about to start
                # without parsing a localised display string.
                "kickoff_ts": getattr(pick.fixture, "start_utc", None),
                # SportyBet's fixture id, captured here so re-pricing this
                # selection is a single call instead of a paginated re-scan of
                # every event on the book.
                "event_id": item.event_id,
                "country": pick.fixture.category,
                "competition": pick.fixture.tournament,
                "home": pick.fixture.home_name,
                "away": pick.fixture.away_name,
                "line": pick.line,
                "model_probability": round(pick.probability, 5),
                "conviction": round(pick.conviction, 5),
                "tier": pick.tier,
                "odds": item.odds,
                "implied_probability": (
                    round(item.implied_probability, 5) if item.implied_probability else None
                ),
                "edge": round(item.edge, 5) if item.edge is not None else None,
                "rationale": pick.rationale,
                "ticket": ticket_label,
                # Filled in later, by the closing-price sweep and by grading.
                "closing_odds": None,
                "closing_captured_at": None,
                "result": None,
            }
        )

    if not rows:
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    logger.info("Recorded %d draw picks to %s", len(rows), path)
    return len(rows)


def load_ledger(path: Path = DEFAULT_LEDGER_PATH) -> list[dict[str, Any]]:
    """Read every recorded pick. Malformed lines are skipped, not fatal."""
    if not path.exists():
        return []

    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping malformed ledger line %d", number)
    return rows


def row_key(row: dict[str, Any]) -> tuple[str, Any]:
    """
    Identity for a ledger row: the selection plus its kickoff.

    Not ``line`` alone — the same two teams meet again, and a rerun on the same
    day legitimately records the same selection twice. Kickoff separates them.
    """
    return (row.get("line") or "", row.get("kickoff_ts") or row.get("kickoff"))


def pending_closing_capture(
    within_seconds: int = 900,
    now_ts: Optional[int] = None,
    path: Path = DEFAULT_LEDGER_PATH,
) -> list[dict[str, Any]]:
    """
    Rows whose kickoff is imminent and whose closing price is not yet stored.

    ``within_seconds`` is the window before kickoff to capture in — 15 minutes
    by default. Earlier is not "closing" and misses late money; later risks the
    market being suspended and returning nothing.

    Rows already past kickoff are excluded rather than captured late. A price
    pulled after the whistle is not a closing price, and silently storing one
    would poison the only signal that reads before the sample matures.
    """
    now_ts = now_ts if now_ts is not None else int(datetime.now(timezone.utc).timestamp())
    pending = []
    for row in load_ledger(path):
        if row.get("closing_odds") is not None:
            continue
        if not row.get("event_id"):
            continue
        kickoff = row.get("kickoff_ts")
        if not isinstance(kickoff, (int, float)):
            continue
        if now_ts <= kickoff <= now_ts + within_seconds:
            pending.append(row)
    return pending


def apply_closing_odds(
    captured: dict[tuple[str, Any], float],
    path: Path = DEFAULT_LEDGER_PATH,
) -> int:
    """
    Write closing prices back into the ledger. Returns rows updated.

    Rewrites the file rather than appending, because a JSONL row has to be
    mutated in place. Written to a temporary file and moved over the original
    so an interrupted sweep cannot truncate the ledger — the sample it holds
    takes weeks to rebuild and cannot be re-fetched from anywhere.
    """
    if not captured:
        return 0

    rows = load_ledger(path)
    if not rows:
        return 0

    now = datetime.now(timezone.utc).isoformat()
    updated = 0
    for row in rows:
        key = row_key(row)
        if key in captured and row.get("closing_odds") is None:
            row["closing_odds"] = captured[key]
            row["closing_captured_at"] = now
            updated += 1

    if not updated:
        return 0

    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    temp.replace(path)

    logger.info("Wrote closing odds for %d rows to %s", updated, path)
    return updated


def summarise(path: Path = DEFAULT_LEDGER_PATH) -> str:
    """
    Report what the sample can and cannot yet support.

    Deliberately blunt about sample size. The failure mode this guards against
    is the one already documented in the repo: an early 14-pick reading that
    looked like signal, drove a calibration change, and turned out to be noise
    once 142 picks existed.
    """
    rows = load_ledger(path)
    if not rows:
        return "Draw ledger is empty — no picks recorded yet."

    graded = [r for r in rows if r.get("result") in ("win", "loss")]
    priced = [r for r in rows if r.get("odds")]
    # Deliberately not restricted to graded rows. Closing line value is the one
    # signal that reads before results exist — gating it on grading would hide
    # it for the six weeks it is meant to cover.
    with_closing = [r for r in rows if r.get("closing_odds") and r.get("odds")]

    lines = [
        "-" * 78,
        "DRAW LEDGER",
        "-" * 78,
        f"recorded: {len(rows)}   priced: {len(priced)}   "
        f"closing captured: {len(with_closing)}   graded: {len(graded)}",
    ]

    if with_closing:
        beat = sum(1 for r in with_closing if r["odds"] > r["closing_odds"])
        drift = sum(
            (r["odds"] - r["closing_odds"]) / r["odds"] for r in with_closing
        ) / len(with_closing)
        lines.append("")
        lines.append(
            f"closing line value: beat the close on {beat}/{len(with_closing)} "
            f"= {beat / len(with_closing):.1%}   average drift {drift:+.2%}"
        )
        if len(with_closing) < 150:
            lines.append(
                f"  ({150 - len(with_closing)} more captures before this is readable)"
            )
        elif beat / len(with_closing) > 0.55:
            lines.append("  Beating the close more often than not — the edge looks real.")
        elif beat / len(with_closing) < 0.45:
            lines.append("  Losing to the close. The model is behind the market, not ahead of it.")
    else:
        lines.append("")
        lines.append(
            "closing line value: nothing captured yet — run tools/closing_sweep.py "
            "before kickoff or this signal is lost permanently."
        )

    if graded:
        wins = sum(1 for r in graded if r["result"] == "win")
        hit_rate = wins / len(graded)
        staked = len(graded)
        returned = sum(r["odds"] for r in graded if r["result"] == "win" and r.get("odds"))
        roi = (returned - staked) / staked if staked else 0.0

        lines.append("")
        lines.append(f"hit rate: {wins}/{len(graded)} = {hit_rate:.1%}")
        lines.append(f"flat-stake ROI: {roi:+.1%}")

        if len(graded) < 150:
            lines.append("")
            lines.append(
                f"{len(graded)} graded picks is too few to read. Hit rate needs 400-500 "
                "before it means anything; closing line value becomes readable at ~150."
            )
        elif len(graded) < 400:
            lines.append("")
            lines.append(
                f"{len(graded)} graded picks — closing line value is readable, hit rate "
                "and ROI are not yet. Do not tune floors on this."
            )
    else:
        lines.append("")
        lines.append("Nothing graded yet, so none of this is evidence about anything.")

    return "\n".join(lines)
