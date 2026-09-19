"""
Tests for the nightly result report.

The cases that matter here are the ones where a wrong answer is worse than no
answer: a card called WON while a leg is still pending, a corner leg graded 0-0
because the league published no statistics, and a streak broken by a day the
ticket simply was not booked.
"""

from __future__ import annotations

import json

import pytest

from services.nightly_report import (
    GradedTicket,
    apply_grades,
    format_report,
    grade_leg,
    group_tickets,
    rows_for_date,
    streaks,
)


def leg(**kw):
    row = {
        "logged_at": "2026-09-18T08:00:00+00:00",
        "ticket": "top_10",
        "booking_code": "AAA111",
        "ticket_legs": 1,
        "ticket_odds": 2.0,
        "match_id": 1,
        "home_name": "Arsenal",
        "away_name": "Chelsea",
        "selection": "Over 1.5",
        "price": 1.3,
        "validated": True,
    }
    row.update(kw)
    return row


# ── Leg grading ──────────────────────────────────────────────────────────


def test_grades_the_measured_markets_from_the_score():
    assert grade_leg(leg(selection="Over 1.5"), (1, 1)) is True
    assert grade_leg(leg(selection="Over 1.5"), (1, 0)) is False
    assert grade_leg(leg(selection="GG"), (2, 1)) is True
    assert grade_leg(leg(selection="1X"), (0, 1)) is False


def test_team_total_grades_against_that_side_only():
    """Booked as "Home Over 1.5" — the wording the booker can resolve — so the
    grader must read the side token, not the club name."""
    row = leg(selection="Home Over 1.5", market="Arsenal Total Goals")
    assert grade_leg(row, (2, 0)) is True      # the home side scored 2
    assert grade_leg(row, (1, 5)) is False     # six goals, but only one at home
    away = leg(selection="Away Over 0.5", market="Chelsea Total Goals")
    assert grade_leg(away, (3, 1)) is True
    assert grade_leg(away, (3, 0)) is False


def test_a_match_total_is_never_read_as_a_team_total():
    """"Over 1.5" is the match total and must keep going to the measured
    grader; only a Home/Away qualifier makes it a team total."""
    assert grade_leg(leg(selection="Over 1.5"), (1, 1)) is True   # 2 match goals
    assert grade_leg(leg(selection="Home Over 1.5"), (1, 1)) is False


def test_corner_leg_needs_statistics_and_is_pending_without_them():
    row = leg(selection="Over 8.5 Corners", validated=False)
    # A finished 2-1 tells us nothing about corners.
    assert grade_leg(row, (2, 1)) is None
    assert grade_leg(row, (2, 1), stats={}) is None
    # A published row with no corner columns is still unknown, never zero.
    assert grade_leg(row, (2, 1), stats={1: {"home_corners": None}}) is None
    assert grade_leg(row, (2, 1), stats={1: {"home_corners": 5, "away_corners": 4}}) is True
    assert grade_leg(row, (2, 1), stats={1: {"home_corners": 4, "away_corners": 4}}) is False


def test_shots_leg_reads_the_shot_columns_not_the_corner_ones():
    row = leg(selection="Over 6.5 Shots On Target", validated=False)
    stats = {1: {"home_corners": 20, "away_corners": 20,
                 "home_shots_on_target": 3, "away_shots_on_target": 2}}
    assert grade_leg(row, (1, 1), stats=stats) is False


def test_unplayed_fixture_is_pending_not_lost():
    assert grade_leg(leg(), None) is None


# ── Ticket status ────────────────────────────────────────────────────────


def test_a_ticket_with_a_pending_leg_is_not_won():
    rows = [
        leg(match_id=1, ticket_legs=2, selection="Over 1.5"),
        leg(match_id=2, ticket_legs=2, selection="Over 1.5"),
    ]
    tickets = group_tickets(rows)
    apply_grades(tickets, {1: (3, 0)})           # match 2 has no score yet
    assert tickets[0].status == "PENDING"
    assert len(tickets[0].pending_legs) == 1


def test_one_lost_leg_settles_the_ticket_even_with_others_pending():
    rows = [
        leg(match_id=1, ticket_legs=2, selection="Over 1.5"),
        leg(match_id=2, ticket_legs=2, selection="Over 1.5"),
    ]
    tickets = group_tickets(rows)
    apply_grades(tickets, {1: (0, 0)})           # already dead
    assert tickets[0].status == "LOST"


def test_all_legs_in_and_won_is_a_win():
    rows = [
        leg(match_id=1, ticket_legs=2),
        leg(match_id=2, ticket_legs=2),
    ]
    tickets = group_tickets(rows)
    apply_grades(tickets, {1: (2, 0), 2: (1, 1)})
    assert tickets[0].status == "WON"


def test_separate_booking_codes_are_separate_tickets():
    """The digest runs three times a day; those are three bets, not one."""
    rows = [
        leg(booking_code="AAA111", ticket_legs=1, match_id=1),
        leg(booking_code="BBB222", ticket_legs=1, match_id=2),
    ]
    tickets = group_tickets(rows)
    assert len(tickets) == 2
    assert {t.booking_code for t in tickets} == {"AAA111", "BBB222"}


# ── Date selection ───────────────────────────────────────────────────────


def test_rows_are_selected_by_local_date_not_utc():
    """23:30 WAT on the 18th is 22:30 UTC on the 18th — same day, and a late
    log must not fall into the previous report."""
    rows = [leg(logged_at="2026-09-18T22:30:00+00:00")]
    assert len(rows_for_date(rows, "2026-09-18", "Africa/Lagos")) == 1
    assert len(rows_for_date(rows, "2026-09-19", "Africa/Lagos")) == 0


# ── Streaks ──────────────────────────────────────────────────────────────


def test_a_day_the_ticket_was_not_booked_does_not_break_the_streak():
    rows = [
        leg(logged_at="2026-09-18T08:00:00+00:00", booking_code="A", match_id=1),
        # nothing on the 17th
        leg(logged_at="2026-09-16T08:00:00+00:00", booking_code="C", match_id=3),
    ]
    scores = {1: (2, 0), 3: (2, 0)}
    runs = streaks(rows, scores, "Africa/Lagos", "2026-09-18")
    assert runs["top_10"] == 2


def test_a_loss_ends_the_streak():
    rows = [
        leg(logged_at="2026-09-18T08:00:00+00:00", booking_code="A", match_id=1),
        leg(logged_at="2026-09-17T08:00:00+00:00", booking_code="B", match_id=2),
        leg(logged_at="2026-09-16T08:00:00+00:00", booking_code="C", match_id=3),
    ]
    scores = {1: (2, 0), 2: (0, 0), 3: (2, 0)}   # the 17th lost
    runs = streaks(rows, scores, "Africa/Lagos", "2026-09-18")
    assert runs["top_10"] == 1


# ── Rendering ────────────────────────────────────────────────────────────


def test_report_names_the_leg_that_broke_the_card():
    rows = [
        leg(match_id=1, ticket_legs=2, home_name="Arsenal", away_name="Chelsea"),
        leg(match_id=2, ticket_legs=2, home_name="Roma", away_name="Lazio"),
    ]
    tickets = group_tickets(rows)
    apply_grades(tickets, {1: (2, 0), 2: (0, 0)})
    out = format_report(tickets, "2026-09-18")
    assert "LOST" in out
    assert "Roma" in out and "Lazio" in out
    assert "Arsenal" not in out          # winning legs are not listed


def test_report_marks_unvalidated_legs():
    rows = [leg(selection="Over 8.5 Corners", validated=False, ticket="top_20")]
    tickets = group_tickets(rows)
    apply_grades(tickets, {1: (1, 1)}, stats={1: {"home_corners": 6, "away_corners": 5}})
    out = format_report(tickets, "2026-09-18")
    assert "unvalidated" in out.lower()


def test_empty_day_renders_without_crashing():
    assert "No tickets" in format_report([], "2026-09-18")
