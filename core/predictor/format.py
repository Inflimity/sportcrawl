"""
Output formatting for SportCrawl's prediction engine.

Two shapes: the bare selection lines that ``core.prediction_parser`` consumes,
and a human-readable report for reviewing picks before they are booked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.predictor.filter import FilterStats
from core.predictor.screen import Pick

if TYPE_CHECKING:  # avoids a circular import at runtime
    from core.predictor.odds import PricedPick


def format_picks(picks: list[Pick]) -> str:
    """
    Render picks as parser-ready text, one selection per line.

    Output feeds straight into ``BookerEngine.book_predictions()``::

        Ajax vs Heerenveen - Over 2.5
        Espanyol vs Levante UD - GG
    """
    return "\n".join(pick.line for pick in picks)


def format_priced(priced: list["PricedPick"]) -> str:
    """
    Render picks against their live market price.

    ``EDGE`` is the model's probability minus the price's implied probability.
    Negative means the market already rates the selection at least as likely as
    the model does — the bet may still win, but it is not value.
    """
    lines = [
        "-" * 78,
        "VALUE vs SPORTYBET PRICE",
        "-" * 78,
        f"{'MATCH':<30} {'PICK':<10} {'RAW':>5} {'CAL':>5} {'ODDS':>6} {'IMPL':>5} {'EDGE':>7}",
    ]

    for item in priced:
        match = item.pick.fixture.label
        if len(match) > 29:
            match = match[:26] + "..."
        if item.odds is None:
            lines.append(
                f"{match:<30} {item.pick.selection:<10} {item.pick.probability:>4.0%} "
                f"{item.calibrated_probability:>4.0%} {'—':>6} {'—':>5}   {item.error or 'no price'}"
            )
            continue
        implied = item.implied_probability or 0.0
        edge = item.edge or 0.0
        flag = "+" if edge > 0 else ""
        lines.append(
            f"{match:<30} {item.pick.selection:<10} {item.pick.probability:>4.0%} "
            f"{item.calibrated_probability:>4.0%} {item.odds:>6.2f} {implied:>4.0%} {flag}{edge:>6.1%}"
        )

    with_edge = [p for p in priced if (p.edge or 0) > 0]
    raw_edge = [p for p in priced if (p.raw_edge or 0) > 0]
    lines.append("-" * 78)
    lines.append(
        f"{len(with_edge)}/{len(priced)} picks show a positive edge after calibration "
        f"({len(raw_edge)} before it)."
    )
    lines.append("RAW = model probability. CAL = corrected for measured overconfidence.")
    lines.append("EDGE uses CAL — it is what the bet is actually worth, not what the model claims.")
    return "\n".join(lines)


def format_report(picks: list[Pick], stats: FilterStats, show_dropped: bool = False) -> str:
    """Render a review table with probabilities, conviction and reasoning."""
    lines: list[str] = []

    lines.append("=" * 78)
    lines.append("SPORTCRAWL PREDICTION SCREEN")
    lines.append("=" * 78)
    lines.append(
        f"Fixtures in file: {stats.total}   tradeable: {stats.kept}   "
        f"picks generated: {len(picks)}"
    )
    lines.append(
        f"Dropped — not allowlisted: {stats.not_allowlisted}, "
        f"excluded competition: {stats.excluded_competition}, "
        f"already started: {stats.already_started}, malformed: {stats.malformed}"
    )
    lines.append("")

    if not picks:
        lines.append("No selection cleared the conviction thresholds.")
        lines.append("That is a normal outcome on a thin fixture list — it is not an error.")
    else:
        lines.append(f"{'#':<3} {'MATCH':<42} {'PICK':<10} {'PROB':>6} {'CONV':>6}")
        lines.append("-" * 78)
        for i, pick in enumerate(picks, 1):
            match = pick.fixture.label
            if len(match) > 41:
                match = match[:38] + "..."
            lines.append(
                f"{i:<3} {match:<42} {pick.selection:<10} "
                f"{pick.probability:>5.1%} {pick.conviction:>5.1%}"
            )
            lines.append(f"    {pick.fixture.category} · {pick.fixture.tournament} · {pick.fixture.start_local}")
            lines.append(f"    {pick.rationale}")
            lines.append("")

        lines.append("-" * 78)
        lines.append("BOOKER INPUT (paste into /book, or pipe to --book):")
        lines.append("-" * 78)
        lines.append(format_picks(picks))

    if show_dropped and stats.dropped_examples:
        lines.append("")
        lines.append("Sample of dropped fixtures:")
        for reason, examples in stats.dropped_examples.items():
            lines.append(f"  {reason}:")
            for example in examples:
                lines.append(f"    - {example}")

    return "\n".join(lines)
