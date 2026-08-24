"""
Prediction engine for SportCrawl.

Turns a raw SofaScore fixture dump into a ranked shortlist of high-conviction
betting selections, formatted as text that ``core.prediction_parser`` accepts.

Pipeline::

    fixtures JSON  ->  filter  ->  enrich  ->  screen  ->  format  ->  booker

Nothing here touches the booking path; the output contract is plain text lines
such as ``Ajax vs Heerenveen - Over 2.5``.
"""

from core.predictor.filter import FilterStats, filter_fixtures
from core.predictor.format import format_picks, format_report
from core.predictor.screen import Pick, screen_fixtures

__all__ = [
    "FilterStats",
    "Pick",
    "filter_fixtures",
    "format_picks",
    "format_report",
    "screen_fixtures",
]
