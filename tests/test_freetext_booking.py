"""
Tests for using the booker as a booking tool: arbitrary markets, typed by hand.

The engine only ever emits six normalized selections, so the booker only ever
had to understand six. Booking a slip a person wrote is a different job — a
SportyBet event carries ~1,000 markets — and three things stood between the two.

**Lines were silently discarded.** The fixture/market split scanned for a market
keyword with a lazy prefix, so it cut at the first keyword-ish token anywhere in
the line. "Ajax vs PSV - Correct Score 2-1" was cut at the "2" of the scoreline,
leaving "Ajax vs PSV - Correct Score" as the fixture, which then split into three
parts on the team delimiters and the whole line was dropped with no message.

**Any colon was treated as the fixture/market separator**, so "Handicap 0:1 Home"
was cut at the 0 — away team "Bournemouth - Handicap 0", market "1 Home".

**Qualified totals were read as plain goals.** "HT Over 0.5", "Early Goals Over
1.5" and "Newcastle Over 1.5" all resolved to full-time goals, because the
Over/Under branch only ever looked at the number.

Markets outside the six fast paths are now matched against the event's own
market descriptions, under a strict rule: every token the user wrote must be
accounted for, the most specific reading wins, and a tie is refused rather than
guessed.
"""

import pytest

from core.market_mapper import resolve_by_description, resolve_market_selection
from core.prediction_parser import (
    MarketCategory,
    parse_prediction_line,
    parse_prediction_text,
    unparsed_lines,
)


def market(mid, desc, outcomes, specifier=None, status=0):
    return {
        "id": str(mid), "desc": desc, "status": status,
        "specifier": specifier,
        "outcomes": [{"id": str(i), "desc": d, "odds": o, "isActive": 1}
                     for i, d, o in outcomes],
    }


# Shaped from a real Newcastle v Bournemouth payload.
EVENT = [
    market(1, "1X2", [("1", "Home", "2.15"), ("2", "Draw", "3.90"), ("3", "Away", "3.00")]),
    market(60100, "1X2 - 2UP", [("1", "Home", "1.97"), ("2", "Draw", "3.90"), ("3", "Away", "3.00")]),
    market(10, "Double Chance", [("9", "Home or Draw", "1.33"), ("10", "Home or Away", "1.29"),
                                 ("11", "Draw or Away", "1.70")]),
    market(11, "Draw No Bet", [("4", "Home", "1.56"), ("5", "Away", "2.40")]),
    market(18, "Over/Under", [("12", "Over 1.5", "1.18"), ("13", "Under 1.5", "5.20")], "total=1.5"),
    market(18, "Over/Under", [("12", "Over 2.5", "1.57"), ("13", "Under 2.5", "2.45")], "total=2.5"),
    market(60180, "Over/Under - Early Goals",
           [("12", "Over 1.5", "1.16"), ("13", "Under 1.5", "5.25")], "minsnr=10|total=1.5"),
    market(19, "Newcastle Over/Under", [("12", "Over 1.5", "1.78"), ("13", "Under 1.5", "2.00")], "total=1.5"),
    market(29, "GG/NG", [("74", "Yes", "1.50"), ("76", "No", "2.60")]),
    market(45, "Correct Score", [("1", "1:0", "8.00"), ("5", "2:1", "9.10"), ("9", "0:0", "11.0")]),
    market(47, "Half Time/Full Time", [("418", "Home/Home", "3.11"), ("420", "Home/Draw", "13.3"),
                                       ("424", "Draw/Home", "5.60")]),
    market(26, "Odd/Even", [("70", "Odd", "1.99"), ("72", "Even", "1.83")]),
    market(16, "Asian Handicap -1.5", [("1714", "Home (-1.5)", "3.60"),
                                       ("1715", "Away (+1.5)", "1.29")], "hcp=-1.5"),
    market(14, "Handicap 0:1", [("1711", "Home (0:1)", "3.70"), ("1713", "Away (0:1)", "1.75")], "hcp=0:1"),
    market(46, "1st Half - Over/Under", [("12", "Over 0.5", "1.29"), ("13", "Under 0.5", "3.50")], "total=0.5"),
    market(166, "Corners - Over/Under", [("12", "Over 9.5", "1.52"), ("13", "Under 9.5", "2.45")], "total=9.5"),
    # Three handicaps at the same line: "Home -1.5" cannot choose between them.
    market(165, "Corner Handicap", [("1714", "Home (-1.5)", "2.15")], "hcp=-1.5"),
    market(900312, "Bookings Handicap", [("1714", "Home (-1.5)", "6.00")], "hcp=-1.5"),
]


def book(text):
    bet = parse_prediction_line(f"Newcastle vs Bournemouth - {text}")
    assert bet is not None, f"{text!r} did not parse"
    return bet, resolve_market_selection(bet, EVENT)


class TestLinesSurviveParsing:
    @pytest.mark.parametrize("text", [
        "Correct Score 2-1", "Handicap 0:1 Home", "Asian Handicap -1.5 Home",
        "HT Over 0.5", "Early Goals Over 1.5", "1/1", "Odd",
        "Newcastle Over 1.5", "Over 9.5 Corners", "DNB 1",
    ])
    def test_the_fixture_is_read_correctly(self, text):
        bet = parse_prediction_line(f"Newcastle vs Bournemouth - {text}")
        assert bet is not None, f"{text!r} was dropped entirely"
        assert (bet.home_team, bet.away_team) == ("Newcastle", "Bournemouth")
        assert bet.raw_selection == text

    def test_a_whole_slip_parses(self):
        slip = "\n".join([
            "Arsenal vs Chelsea - Over 2.5",
            "Man City vs Liverpool : GG",
            "Real Madrid v Barcelona -> 1X",
            "Ajax vs PSV - Correct Score 2-1",
            "Roma vs Lazio - Asian Handicap -1.5 Home",
            "Inter vs Milan - HT Over 0.5",
        ])
        assert len(parse_prediction_text(slip)) == 6
        assert unparsed_lines(slip) == []

    def test_unreadable_lines_are_reported_rather_than_vanishing(self):
        slip = "Arsenal vs Chelsea - Over 2.5\nthis is not a bet at all\n"
        assert len(parse_prediction_text(slip)) == 1
        assert unparsed_lines(slip) == ["this is not a bet at all"]


class TestMarketsBookToTheRightPlace:
    @pytest.mark.parametrize("text, market_id, outcome_id", [
        ("Over 2.5", "18", "12"),
        ("GG", "29", "74"),
        ("1X", "10", "9"),
        ("DNB 1", "11", "4"),
        ("Correct Score 2-1", "45", "5"),
        ("Correct Score 2:1", "45", "5"),
        ("1/1", "47", "418"),
        ("Odd", "26", "70"),
        ("Even", "26", "72"),
        ("Asian Handicap -1.5 Home", "16", "1714"),
        ("Handicap 0:1 Home", "14", "1711"),
        ("HT Over 0.5", "46", "12"),
        ("Early Goals Over 1.5", "60180", "12"),
        ("2UP Home", "60100", "1"),
        ("Newcastle Over 1.5", "19", "12"),
        ("Over 9.5 Corners", "166", "12"),
    ])
    def test_resolves_to_the_market_the_wording_names(self, text, market_id, outcome_id):
        _, resolved = book(text)
        assert resolved is not None, f"{text!r} was refused"
        assert (resolved["marketId"], resolved["outcomeId"]) == (market_id, outcome_id)
        live = [o for m in EVENT
                if str(m["id"]) == resolved["marketId"]
                and (m.get("specifier") or "") == (resolved.get("specifier") or "")
                for o in m["outcomes"] if str(o["id"]) == resolved["outcomeId"]]
        assert live and resolved["odds"] == live[0]["odds"]

    def test_early_goals_is_not_the_plain_goals_market(self):
        _, plain = book("Over 1.5")
        _, early = book("Early Goals Over 1.5")
        assert plain["marketId"] == "18"
        assert early["marketId"] == "60180"
        assert plain["odds"] != early["odds"]

    def test_a_team_total_is_not_the_match_total(self):
        _, match_total = book("Over 1.5")
        _, team_total = book("Newcastle Over 1.5")
        assert (match_total["marketId"], team_total["marketId"]) == ("18", "19")


class TestItRefusesRatherThanGuesses:
    def test_an_equally_good_reading_is_refused(self):
        """"Home -1.5" fits Asian Handicap, Corner Handicap and Bookings
        Handicap identically. Picking one would book a market never asked for."""
        _, resolved = book("Home -1.5")
        assert resolved is None

    def test_naming_the_market_resolves_the_ambiguity(self):
        _, resolved = book("Asian Handicap -1.5 Home")
        assert resolved is not None and resolved["marketId"] == "16"

    def test_a_known_market_never_falls_through_to_similar_wording(self):
        """
        "Over 9.5" is goals, and this event has no 9.5 goals line — but it does
        have corners at 9.5. An ungated description match returned the corners
        market for a goals bet, which is the exact silent swap this work removes.
        """
        bet, resolved = book("Over 9.5")
        assert bet.market_category == MarketCategory.OVER_UNDER
        assert resolved is None

    def test_wording_that_matches_nothing_is_refused(self):
        assert resolve_by_description("player to score a hat trick", EVENT) is None

    def test_a_suspended_market_loses_to_an_open_one(self):
        event = [
            market(26, "Odd/Even", [("70", "Odd", "9.99")], status=1),
            market(26, "Odd/Even", [("70", "Odd", "1.99")], status=0),
        ]
        assert resolve_by_description("Odd", event)["odds"] == "1.99"
