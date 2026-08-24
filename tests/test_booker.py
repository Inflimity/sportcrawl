"""
Unit and Integration Tests for SportyBet Prediction Booker Engine.
"""

import pytest
from core.prediction_parser import MarketCategory, parse_prediction_line, parse_prediction_text
from core.team_matcher import normalize_team_name, team_similarity, match_fixture
from core.market_mapper import resolve_market_selection, SPORTYBET_MARKETS
from core.booker_engine import BookerEngine


class TestPredictionParser:
    """Test parsing varied prediction formats."""

    def test_over_under_standard(self):
        text = "Arsenal vs Chelsea - Over 2.5"
        bet = parse_prediction_line(text)
        assert bet is not None
        assert bet.home_team == "Arsenal"
        assert bet.away_team == "Chelsea"
        assert bet.market_category == MarketCategory.OVER_UNDER
        assert bet.selection == "Over 2.5"

    def test_1x2_home_win(self):
        text = "Real Madrid - Barcelona : 1"
        bet = parse_prediction_line(text)
        assert bet is not None
        assert bet.home_team == "Real Madrid"
        assert bet.away_team == "Barcelona"
        assert bet.market_category == MarketCategory.MATCH_WINNER
        assert bet.selection == "1"

    def test_1x2_textual(self):
        text = "1. Liverpool vs Man City -> Home Win"
        bet = parse_prediction_line(text)
        assert bet is not None
        assert bet.home_team == "Liverpool"
        assert bet.away_team == "Man City"
        assert bet.market_category == MarketCategory.MATCH_WINNER
        assert bet.selection == "1"

    def test_btts_gg(self):
        text = "🔥 Inter Milan vs Juventus => GG @1.85"
        bet = parse_prediction_line(text)
        assert bet is not None
        assert bet.home_team == "Inter Milan"
        assert bet.away_team == "Juventus"
        assert bet.market_category == MarketCategory.BTTS
        assert bet.selection == "GG"
        assert bet.odds == 1.85

    def test_double_chance(self):
        text = "Aston Villa vs Wolves | 1X"
        bet = parse_prediction_line(text)
        assert bet is not None
        assert bet.home_team == "Aston Villa"
        assert bet.away_team == "Wolves"
        assert bet.market_category == MarketCategory.DOUBLE_CHANCE
        assert bet.selection == "1X"

    def test_draw_no_bet(self):
        text = "Bayern Munich vs Dortmund - DNB 1"
        bet = parse_prediction_line(text)
        assert bet is not None
        assert bet.home_team == "Bayern Munich"
        assert bet.away_team == "Dortmund"
        assert bet.market_category == MarketCategory.DRAW_NO_BET
        assert bet.selection == "DNB 1"

    def test_parentheses_format(self):
        text = "PSG vs Marseille (Over 3.5)"
        bet = parse_prediction_line(text)
        assert bet is not None
        assert bet.home_team == "PSG"
        assert bet.away_team == "Marseille"
        assert bet.market_category == MarketCategory.OVER_UNDER
        assert bet.selection == "Over 3.5"

    def test_multiline_prediction_text(self):
        text = """
        🎯 VIP TIPS FOR TODAY 🎯
        1. Arsenal vs Chelsea - Over 2.5
        2. Real Madrid vs Barcelona - 1
        3. Inter vs Juventus - GG
        4. Man City vs Liverpool - 1X
        Join our VIP channel @tips
        """
        bets = parse_prediction_text(text)
        assert len(bets) == 4
        assert bets[0].home_team == "Arsenal"
        assert bets[1].market_category == MarketCategory.MATCH_WINNER
        assert bets[2].selection == "GG"
        assert bets[3].selection == "1X"


class TestTeamMatcher:
    """Test team similarity and alias matching."""

    def test_aliases(self):
        assert team_similarity("Man Utd", "Manchester United") >= 0.8
        assert team_similarity("Man City", "Manchester City FC") >= 0.8
        assert team_similarity("PSG", "Paris Saint-Germain") >= 0.8
        assert team_similarity("Wolves", "Wolverhampton Wanderers") >= 0.8

    def test_match_fixture_lookup(self):
        candidates = [
            {"homeTeamName": "Arsenal FC", "awayTeamName": "Chelsea FC", "eventId": "ev_1"},
            {"homeTeamName": "Liverpool FC", "awayTeamName": "Manchester City", "eventId": "ev_2"},
        ]
        match, score = match_fixture("Arsenal", "Chelsea", candidates)
        assert match is not None
        assert match["eventId"] == "ev_1"
        assert score >= 0.8


class TestMarketMapper:
    """Test market and outcome resolution."""

    def test_static_resolution(self):
        bet = parse_prediction_line("Arsenal vs Chelsea - Over 2.5")
        res = resolve_market_selection(bet)
        assert res is not None
        assert res["marketId"] == "18"
        assert res["outcomeId"] == "12"  # Over
        assert "total=2.5" in res["specifier"]

    def test_btts_resolution(self):
        bet = parse_prediction_line("Inter vs Milan - GG")
        res = resolve_market_selection(bet)
        assert res is not None
        assert res["marketId"] == "29"
        assert res["outcomeId"] == "74"


class TestBookerEngine:
    """Test BookerEngine formatting."""

    def test_telegram_response_formatting(self):
        from services.sportybet_service import BookingResult, BookedSelection
        parsed = parse_prediction_text("Arsenal vs Chelsea - Over 2.5\nReal Madrid vs Barca : 1")
        result = BookingResult(
            success=True,
            booking_code="SB8899",
            total_odds="3.45",
            selections_count=2,
            booked_selections=[
                BookedSelection("Arsenal", "Chelsea", "OVER_UNDER", "Over 2.5", "1.75"),
                BookedSelection("Real Madrid", "Barca", "1X2", "1", "1.97"),
            ],
            share_url="https://www.sportybet.com/ng/m/share/SB8899",
        )
        msg = BookerEngine.format_telegram_response(result, parsed)
        assert "SB8899" in msg
        assert "3.45" in msg
        assert "Arsenal vs Chelsea" in msg
        assert "SportyBet" in msg
