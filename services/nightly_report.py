"""
Nightly ticket result report: did today's tickets win?

WHAT THIS ANSWERS, AND WHAT IT DOES NOT
---------------------------------------
It grades **what the engine booked and logged**. `core/ticket_log.py` writes a
row per leg at booking time with the price it was struck at; this reads today's
rows, fetches the real outcomes, and says WON / LOST / PENDING per ticket.

It does not know about legs swapped by hand between tickets, or about Early
Goals being booked in place of the logged market. Those choices happen after
the log is written, so a ticket this report calls WON is the engine's ticket,
not necessarily the slip that was played. The report says so in one line rather
than implying an accuracy it does not have.

OUTCOMES COME FROM SOFASCORE, NEVER FROM THE DATABASE
-----------------------------------------------------
The local database never records about 39% of results, and the gap skews toward
low-scoring competitions — so grading goals markets from it silently inflates
every Over. `tools.ticket_ledger.fetch_outcomes` reads team histories straight
from SofaScore and caches settled scores to disk, so a finished match costs one
request ever. This module reuses it rather than reimplementing the rule.

WHY 23:50 AND WHAT PENDING MEANS
--------------------------------
A 22:00 WAT kickoff is not finished at 23:50, and a report that waited for
every fixture would arrive at random times or not at all. So the report fires
on the clock and marks anything unfinished PENDING, with the count stated up
front. A ticket with one pending leg is UNDECIDED, not won — a card is only
won when every leg is in.
"""

from __future__ import annotations

import html
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("SportCrawl.NightlyReport")

# How a ticket name in the log reads in the report.
TICKET_LABELS = {
    "top_10": "Ticket 1 · Top 10 Bankers",
    "top_20": "Ticket 2 · Top 20 Mega",
    "two_odds": "Ticket 4 · 2 Odds",
}


def label_for(name: str) -> str:
    if name in TICKET_LABELS:
        return TICKET_LABELS[name]
    if name.startswith("draw_"):
        return f"Ticket 3 · Draw {name[5:]}"
    return name.replace("_", " ").title()


# --------------------------------------------------------------------------
# Grading
# --------------------------------------------------------------------------


def grade_leg(row: dict[str, Any], score: Optional[tuple[int, int]],
              stats: Optional[dict[int, dict[str, Any]]] = None) -> Optional[bool]:
    """
    Did this one leg win? None when it cannot be decided yet.

    Three families, because the Top 20 now carries markets the final score
    alone cannot settle:

    - the seven measured markets, graded by `backtest_markets.GRADERS`
    - team totals ("Newcastle Over 1.5"), graded from the side's own score
    - corners and shots on target, which need the statistics row and return
      None when the match published none. An unpublished statistic is unknown,
      never zero — treating a missing corner count as 0 would mark every Over
      a loss and manufacture a losing streak out of a coverage gap.
    """
    from tools.backtest_markets import grade

    sel = (row.get("selection") or "").strip()
    if not sel:
        return None

    # 1. Corners / shots — need the statistics row, not the score.
    lowered = sel.lower()
    if "corner" in lowered or "shot" in lowered:
        if not stats:
            return None
        srow = stats.get(int(row.get("match_id") or 0))
        if not srow:
            return None
        if "corner" in lowered:
            h, a = srow.get("home_corners"), srow.get("away_corners")
        else:
            h, a = srow.get("home_shots_on_target"), srow.get("away_shots_on_target")
        if h is None or a is None:
            return None
        line = _line_in(sel)
        if line is None:
            return None
        return (h + a) > line if "over" in lowered else (h + a) < line

    if score is None:
        return None
    home_score, away_score = score

    # 2. Team totals — "Home Over 1.5" / "Away Over 0.5".
    #
    #    The side is written as Home/Away rather than as the club's name
    #    because that is the only wording SportyBet's market descriptions can
    #    resolve (see extra_markets.screen_team_goals). The club name is
    #    carried on `market` instead, so the digest still reads naturally.
    #
    #    Anchored with a word boundary on the side token so a market whose
    #    description happens to contain "home" cannot be read as a team total.
    for side, own in (("home", home_score), ("away", away_score)):
        if re.match(rf"^{side}\s+(over|under)\b", lowered):
            line = _line_in(sel)
            if line is None:
                return None
            return own > line if "over" in lowered else own < line

    # 3. The measured seven.
    return grade(sel, home_score, away_score)


def _line_in(selection: str) -> Optional[float]:
    """The .5 line inside a selection string, or None if it carries none."""
    m = re.search(r"(\d+(?:\.\d+)?)", selection)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Ticket assembly
# --------------------------------------------------------------------------


@dataclass
class GradedTicket:
    """One booked slip, and how it settled."""

    name: str
    booking_code: Optional[str]
    logged_at: str
    odds: Optional[float]
    legs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def label(self) -> str:
        return label_for(self.name)

    @property
    def won_legs(self) -> list[dict[str, Any]]:
        return [l for l in self.legs if l.get("_won") is True]

    @property
    def lost_legs(self) -> list[dict[str, Any]]:
        return [l for l in self.legs if l.get("_won") is False]

    @property
    def pending_legs(self) -> list[dict[str, Any]]:
        return [l for l in self.legs if l.get("_won") is None]

    @property
    def status(self) -> str:
        """WON only when every leg is in and every leg won."""
        if self.lost_legs:
            return "LOST"          # decided the moment one leg fails
        if self.pending_legs:
            return "PENDING"
        return "WON" if self.legs else "EMPTY"

    @property
    def unvalidated_legs(self) -> list[dict[str, Any]]:
        return [l for l in self.legs if l.get("validated") is False]


def group_tickets(rows: list[dict[str, Any]]) -> list[GradedTicket]:
    """
    Group logged legs into the slips they were booked as.

    Keyed on the booking code where there is one: that is the slip's real
    identity, and the digest runs three times a day, so grouping on the ticket
    name alone would merge three different bets into one impossible 60-leg
    card. Unbooked tickets fall back to their log timestamp.
    """
    buckets: dict[tuple, GradedTicket] = {}
    for row in rows:
        name = row.get("ticket") or "?"
        key = (name, row.get("booking_code") or row.get("logged_at"))
        tk = buckets.get(key)
        if tk is None:
            tk = GradedTicket(
                name=name,
                booking_code=row.get("booking_code"),
                logged_at=row.get("logged_at") or "",
                odds=row.get("ticket_odds"),
            )
            buckets[key] = tk
        tk.legs.append(row)
    return list(buckets.values())


def rows_for_date(rows: list[dict[str, Any]], date_str: str, tz: str) -> list[dict[str, Any]]:
    """Legs logged on the given local date."""
    zone = ZoneInfo(tz)
    out = []
    for row in rows:
        stamp = row.get("logged_at")
        if not stamp:
            continue
        try:
            dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt.astimezone(zone).strftime("%Y-%m-%d") == date_str:
            out.append(row)
    return out


def apply_grades(
    tickets: list[GradedTicket],
    scores: dict[int, tuple[int, int]],
    stats: Optional[dict[int, dict[str, Any]]] = None,
) -> None:
    """Annotate every leg in place with ``_won``."""
    for tk in tickets:
        for leg in tk.legs:
            mid = leg.get("match_id")
            leg["_won"] = grade_leg(
                leg, scores.get(int(mid)) if mid is not None else None, stats
            )
            leg["_score"] = scores.get(int(mid)) if mid is not None else None


def streaks(
    all_rows: list[dict[str, Any]],
    scores: dict[int, tuple[int, int]],
    tz: str,
    up_to: str,
    stats: Optional[dict[int, dict[str, Any]]] = None,
    max_days: int = 60,
) -> dict[str, int]:
    """
    Consecutive settled days each ticket has won, counting back from ``up_to``.

    A day counts only if the ticket settled that day — a day the ticket was not
    booked, or is still pending, ends nothing and is skipped rather than
    treated as a loss. Counting a blank day as a break would understate a real
    run; counting it as a win would invent one.
    """
    zone = ZoneInfo(tz)
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in all_rows:
        stamp = row.get("logged_at")
        if not stamp:
            continue
        try:
            dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        by_day[dt.astimezone(zone).strftime("%Y-%m-%d")].append(row)

    out: dict[str, int] = {}
    start = datetime.strptime(up_to, "%Y-%m-%d").date()

    names = {r.get("ticket") for r in all_rows if r.get("ticket")}
    for name in names:
        run = 0
        for back in range(max_days):
            day = (start - timedelta(days=back)).strftime("%Y-%m-%d")
            day_rows = [r for r in by_day.get(day, []) if r.get("ticket") == name]
            if not day_rows:
                continue                      # not booked that day; not a break
            day_tickets = group_tickets(day_rows)
            apply_grades(day_tickets, scores, stats)
            settled = [t for t in day_tickets if t.status in ("WON", "LOST")]
            if not settled:
                continue                      # still pending; not a break
            if all(t.status == "WON" for t in settled):
                run += 1
            else:
                break
        out[name] = run
    return out


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


_STATUS_ICON = {"WON": "✅", "LOST": "❌", "PENDING": "⏳", "EMPTY": "—"}


def _esc(text: Any) -> str:
    return html.escape(str(text if text is not None else ""), quote=False)


def format_report(
    tickets: list[GradedTicket],
    date_str: str,
    runs: Optional[dict[str, int]] = None,
    show_legs: bool = True,
) -> str:
    """
    Render the nightly report as Telegram HTML.

    Losing legs are always named. A report that says only "LOST" tells you
    nothing you can act on; the leg that broke the card is the entire content
    of a losing night, and on a winning night the prices are.
    """
    runs = runs or {}
    if not tickets:
        return (
            f"🌙 <b>Nightly Results — {_esc(date_str)}</b>\n\n"
            "No tickets were logged today.\n"
            "<i>Either no digest ran, or no fixture cleared the screening floors.</i>"
        )

    order = {"top_10": 0, "top_20": 1, "two_odds": 2}
    tickets = sorted(
        tickets, key=lambda t: (order.get(t.name, 3), t.name, t.logged_at)
    )

    won = sum(1 for t in tickets if t.status == "WON")
    lost = sum(1 for t in tickets if t.status == "LOST")
    pending = sum(1 for t in tickets if t.status == "PENDING")

    head = [f"🌙 <b>Nightly Results — {_esc(date_str)}</b>", ""]
    summary = f"<b>{won} won · {lost} lost</b>"
    if pending:
        summary += f" · {pending} still pending"
    head.append(summary)
    head.append("")

    body: list[str] = []
    for tk in tickets:
        icon = _STATUS_ICON.get(tk.status, "—")
        line = f"{icon} <b>{_esc(tk.label)}</b> — {tk.status}"
        if tk.booking_code:
            line += f"  <code>{_esc(tk.booking_code)}</code>"
        body.append(line)

        counts = (
            f"   {len(tk.won_legs)}/{len(tk.legs)} legs in"
            + (f", {len(tk.pending_legs)} pending" if tk.pending_legs else "")
        )
        if tk.odds:
            counts += f"   ·   @{tk.odds:.2f}"
        body.append(counts)

        if tk.unvalidated_legs:
            body.append(
                f"   <i>{len(tk.unvalidated_legs)} unvalidated leg(s) — "
                f"corners/shots/team totals, no graded rate yet</i>"
            )

        if show_legs:
            for leg in tk.lost_legs:
                score = leg.get("_score")
                sc = f" ({score[0]}-{score[1]})" if score else ""
                body.append(
                    f"   ❌ {_esc(leg.get('home_name'))} v {_esc(leg.get('away_name'))}"
                    f" — {_esc(leg.get('selection'))}{sc}"
                )
            for leg in tk.pending_legs:
                body.append(
                    f"   ⏳ {_esc(leg.get('home_name'))} v {_esc(leg.get('away_name'))}"
                    f" — {_esc(leg.get('selection'))}"
                )

        run = runs.get(tk.name, 0)
        if tk.status == "WON" and run > 1:
            body.append(f"   🔥 {run} days running")
        body.append("")

    foot = [
        "<i>Graded from SofaScore final scores, not the local database.</i>",
        "<i>These are the tickets the engine booked and logged. Legs you moved</i>",
        "<i>between tickets by hand, or booked as Early Goals, are not tracked here.</i>",
    ]
    if pending:
        foot.insert(0, "<i>Pending legs are matches not finished at report time.</i>")

    return "\n".join(head + body + foot)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


async def build_nightly_report(
    date_str: Optional[str] = None,
    tz: str = "Africa/Lagos",
    log_path: str = "logs/tickets.jsonl",
    cache_path: str = "logs/outcomes.json",
    stats_cache: Optional[str] = None,
    show_legs: bool = True,
) -> tuple[str, list[GradedTicket]]:
    """
    Build the report for one local date. Returns (telegram_html, tickets).

    Fetches only what it must: settled scores are cached to disk by
    `ticket_ledger.fetch_outcomes`, and corner statistics are read from the
    cache the prediction run already banked rather than re-fetched. If a
    corner leg's match published no statistics it stays PENDING, which is the
    truthful state — not a loss.
    """
    from tools.ticket_ledger import fetch_outcomes, load_log

    zone = ZoneInfo(tz)
    date_str = date_str or datetime.now(zone).strftime("%Y-%m-%d")

    try:
        all_rows = load_log(log_path)
    except FileNotFoundError:
        logger.warning("No ticket log at %s; nothing to report.", log_path)
        return format_report([], date_str), []

    today_rows = rows_for_date(all_rows, date_str, tz)
    if not today_rows:
        return format_report([], date_str), []

    # Scores for today's legs plus whatever the streak walk-back needs.
    scores = await fetch_outcomes(all_rows, cache_path)

    # Corner and shot legs cannot be graded from a scoreline. The prediction
    # run banks statistics for teams' PAST matches, which never includes the
    # fixture being graded here — so without this fetch a corner leg would sit
    # PENDING forever and quietly stall every streak it appears in.
    stats = None
    stat_legs = [
        r for r in today_rows
        if "corner" in (r.get("selection") or "").lower()
        or "shot" in (r.get("selection") or "").lower()
    ]
    if stat_legs:
        from core.predictor.stats_form import append_stats_cache, load_stats_cache

        stats = load_stats_cache(stats_cache)
        # Only matches that have actually finished: an unfinished match has no
        # statistics, and asking anyway spends a request to learn nothing.
        missing = sorted({
            int(r["match_id"]) for r in stat_legs
            if r.get("match_id") is not None
            and int(r["match_id"]) not in stats
            and int(r["match_id"]) in scores
        })
        if missing:
            try:
                from core.predictor.match_stats import fetch_match_stats

                fetched = await fetch_match_stats(missing)
                append_stats_cache(fetched.values(), stats_cache)
                for mid, line in fetched.items():
                    stats[int(mid)] = {
                        "match_id": int(mid),
                        "home_corners": line.home_corners,
                        "away_corners": line.away_corners,
                        "home_shots_on_target": line.home_shots_on_target,
                        "away_shots_on_target": line.away_shots_on_target,
                    }
                logger.info(
                    "Nightly report: fetched statistics for %d/%d corner legs.",
                    len(fetched), len(missing),
                )
            except Exception as e:  # noqa: BLE001
                # Those legs stay PENDING, which is honest. A statistics
                # outage must not take the whole report down with it.
                logger.warning(
                    "Could not fetch match statistics (%s); corner legs stay pending.", e
                )

    tickets = group_tickets(today_rows)
    apply_grades(tickets, scores, stats)
    runs = streaks(all_rows, scores, tz, date_str, stats)

    return format_report(tickets, date_str, runs, show_legs), tickets
