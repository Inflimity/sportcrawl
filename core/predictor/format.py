"""
Output formatting for SportCrawl's prediction engine.

Two shapes: the bare selection lines that ``core.prediction_parser`` consumes,
and a human-readable report for reviewing picks before they are booked.
"""

from __future__ import annotations

from core.predictor.filter import FilterStats
from core.predictor.screen import Pick


def format_picks(picks: list[Pick]) -> str:
    """
    Render picks as parser-ready text, one selection per line.

    Output feeds straight into ``BookerEngine.book_predictions()``::

        Ajax vs Heerenveen - Over 2.5
        Espanyol vs Levante UD - GG
    """
    return "\n".join(pick.line for pick in picks)


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
