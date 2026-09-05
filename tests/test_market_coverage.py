"""
Regression tests for markets that were dropped, faked, or silently swapped.

Three separate defects sat between a selection and the betslip.

**The resolver invented selections.** When a market was missing from the event
payload the dynamic lookup fell through to the static table, which returned a
market id, an outcome id and a hardcoded 1.50. Verified against the live card:
lower-league events carry ~110 markets and one carried a single 1X2, so a GG
leg on such a fixture "resolved" to market 29 outcome 74 — a market that event
does not have — and that fake 1.50 was multiplied into the ticket's total odds
and written to the price log.

**A corners line booked goals.** The Over/Under branch matched "Over N" and
emitted ``total=N`` whatever followed it, so "Over 9.5 Corners" resolved
against market 18 (goals). The digest said corners; the slip settled on goals.

**Two static outcome ids were wrong.** Live payloads give market 29 GG/NG as
Yes=74 / No=76 and market 11 Draw No Bet as Home=4 / Away=5. The table recorded
No=75 and DNB Home=1 / Away=2.
"""

import pytest

from core.market_mapper import resolve_market_selection
from core.prediction_parser import MarketCategory, parse_prediction_line


def market(mid, outcomes, specifier=None, status=0, product=3):
    m = {"id": str(mid), "status": status, "product": product,
         "outcomes": [{"id": str(i), "desc": d, "odds": o} for i, d, o in outcomes]}
    if specifier is not None:
        m["specifier"] = specifier
    return m


# Shaped like a real SportyBet event payload, ids and descs taken from a live one.
FULL_EVENT = [
    market(1, [("1", "Home", "2.15"), ("2", "Draw", "3.90"), ("3", "Away", "3.00")]),
    market(10, [("9", "Home or Draw", "1.37"), ("10", "Home or Away", "1.25"),
                ("11", "Draw or Away", "1.67")]),
    market(18, [("12", "Over 1.5", "1.16"), ("13", "Under 1.5", "5.25")], "total=1.5"),
    market(18, [("12", "Over 2.5", "1.47"), ("13", "Under 2.5", "2.70")], "total=2.5"),
    market(29, [("74", "Yes", "1.45"), ("76", "No", "2.75")]),
    market(166, [("12", "Over 9.5", "1.52"), ("13", "Under 9.5", "2.45")], "total=9.5"),
    market(162, [("1", "Home", "1.75"), ("2", "Draw", "8.75"), ("3", "Away", "2.40")]),
]

THIN_EVENT = [market(1, [("1", "Home", "1.87"), ("2", "Draw", "4.88"), ("3", "Away", "3.57")])]


def resolve(text, markets):
    bet = parse_prediction_line(f"Newcastle vs Bournemouth - {text}")
    assert bet is not None, text
    return bet, resolve_market_selection(bet, markets)


class TestEveryMarketResolvesOnAFullEvent:
    @pytest.mark.parametrize(
        "text, market_id, outcome_id",
        [
            ("Over 1.5", "18", "12"),
            ("Over 2.5", "18", "12"),
            ("Under 2.5", "18", "13"),
            ("GG", "29", "74"),
            ("NG", "29", "76"),
            ("1", "1", "1"),
            ("X", "1", "2"),
            ("2", "1", "3"),
            ("1X", "10", "9"),
            ("X2", "10", "11"),
            ("Over 9.5 Corners", "166", "12"),
        ],
    )
    def test_resolves_to_a_market_the_event_actually_has(self, text, market_id, outcome_id):
        _, resolved = resolve(text, FULL_EVENT)
        assert resolved is not None, f"{text} was dropped"
        assert resolved["marketId"] == market_id
        assert resolved["outcomeId"] == outcome_id
        # The resolved triple must exist on the event, not merely be well-formed.
        live = [
            o for m in FULL_EVENT
            if str(m["id"]) == resolved["marketId"]
            and (m.get("specifier") or "") == (resolved.get("specifier") or "")
            for o in m["outcomes"] if str(o["id"]) == resolved["outcomeId"]
        ]
        assert live, f"{text} resolved to a selection not present on the event"
        assert resolved["odds"] == live[0]["odds"]


class TestNoFabricatedSelections:
    @pytest.mark.parametrize("text", ["GG", "NG", "Over 1.5", "Over 2.5", "1X", "X2",
                                      "Over 9.5 Corners"])
    def test_a_market_the_event_lacks_is_reported_missing_not_invented(self, text):
        _, resolved = resolve(text, THIN_EVENT)
        assert resolved is None, f"{text} was invented on an event without that market"

    def test_the_one_market_a_thin_event_does_have_still_books(self):
        _, resolved = resolve("1", THIN_EVENT)
        assert resolved is not None
        assert (resolved["marketId"], resolved["outcomeId"]) == ("1", "1")
        assert resolved["odds"] == "1.87"


class TestCornersAreNotGoals:
    def test_a_corners_line_is_not_classified_as_goals(self):
        bet, resolved = resolve("Over 9.5 Corners", FULL_EVENT)
        assert bet.market_category == MarketCategory.CORNERS
        assert resolved["marketId"] == "166"

    def test_a_goals_line_is_unaffected(self):
        bet, resolved = resolve("Over 2.5", FULL_EVENT)
        assert bet.market_category == MarketCategory.OVER_UNDER
        assert resolved["marketId"] == "18"

    def test_corners_and_goals_on_the_same_line_number_differ(self):
        """Both markets use outcome 12 for Over; only the market id separates them."""
        _, goals = resolve("Over 9.5", FULL_EVENT)
        _, corners = resolve("Over 9.5 Corners", FULL_EVENT)
        assert corners is not None and corners["marketId"] == "166"
        # 9.5 goals is not listed on this event, so it must not resolve at all.
        assert goals is None

    @pytest.mark.parametrize("text", ["Over 3.5 Cards", "Over 12.5 Shots",
                                      "Over 19.5 Fouls", "Over 1.5 Offsides"])
    def test_an_unsupported_subject_is_refused_rather_than_read_as_goals(self, text):
        bet = parse_prediction_line(f"Newcastle vs Bournemouth - {text}")
        assert bet is not None
        assert bet.market_category == MarketCategory.UNKNOWN
        assert resolve_market_selection(bet, FULL_EVENT) is None


class TestSpecifierMatchingIsExact:
    def test_a_line_not_listed_does_not_borrow_another(self):
        """`target_line in spec` matched "total=1.5" against a 1.5 line inside
        "total=11.5", and matched compound specifiers like "minute=15|total=0.5"."""
        event = [market(18, [("12", "Over 11.5", "9.00")], "total=11.5"),
                 market(18, [("12", "Over 0.5", "1.02")], "minute=15|total=0.5")]
        _, resolved = resolve("Over 1.5", event)
        assert resolved is None


class TestOpenMarketCopyIsPreferred:
    def test_a_suspended_copy_loses_to_an_open_one(self):
        """
        SportyBet lists most markets twice on an upcoming event — a prematch
        copy and a live copy suspended until kickoff — with different prices.
        """
        event = [
            market(29, [("74", "Yes", "9.99"), ("76", "No", "9.99")], status=1, product=1),
            market(29, [("74", "Yes", "1.45"), ("76", "No", "2.75")], status=0, product=3),
        ]
        _, resolved = resolve("GG", event)
        assert resolved is not None
        assert resolved["odds"] == "1.45"


class TestTicketMarketDiversity:
    """
    A three-leg ticket of three Over 1.5 legs is one model estimate entered
    three times. ``max_per_market`` bounds that; it is off by default because
    Over 1.5 is the only market here with a measured hit rate.
    """

    @staticmethod
    def _priced():
        from core.predictor.filter import Fixture
        from core.predictor.odds import PricedPick
        from core.predictor.screen import Pick

        out = []
        for i, (sel, prob, odds) in enumerate([
            # Three Over 1.5 legs multiply to 1.95 — under the 2.00 cap and past
            # the 0.85 target — and are jointly safer than any mixed combination,
            # so an uncapped search takes all three.
            ("Over 1.5", 0.82, 1.24), ("Over 1.5", 0.81, 1.25), ("Over 1.5", 0.80, 1.26),
            ("GG", 0.62, 1.55), ("GG", 0.61, 1.58),
        ]):
            fx = Fixture(match_id=i, tournament="Premier League", category="England",
                         home_name=f"H{i}", away_name=f"A{i}", home_id=i * 2, away_id=i * 2 + 1,
                         start_utc="2026-09-05T18:00:00+00:00", start_local="")
            pick = Pick(fixture=fx, market="m", selection=sel, probability=prob,
                        conviction=prob, rationale="")
            out.append(PricedPick(pick=pick, odds=odds))
        return out

    def test_uncapped_the_ticket_may_be_all_one_market(self):
        from core.predictor.tickets import build_capped_ticket
        ticket = build_capped_ticket(self._priced(), max_combined_odds=2.0,
                                     max_legs=3, min_legs=2)
        assert ticket is not None
        assert len({leg.selection for leg in ticket.legs}) == 1

    def test_a_cap_of_two_forces_a_second_market_in(self):
        from core.predictor.tickets import build_capped_ticket
        ticket = build_capped_ticket(self._priced(), max_combined_odds=2.0,
                                     max_legs=3, min_legs=2, max_per_market=2)
        assert ticket is not None
        counts = {}
        for leg in ticket.legs:
            counts[leg.selection] = counts.get(leg.selection, 0) + 1
        assert max(counts.values()) <= 2

    def test_one_leg_per_fixture_still_holds(self):
        from core.predictor.tickets import build_capped_ticket
        ticket = build_capped_ticket(self._priced(), max_combined_odds=2.0,
                                     max_legs=3, min_legs=2, max_per_market=2)
        keys = [(leg.fixture.home_name, leg.fixture.away_name) for leg in ticket.legs]
        assert len(keys) == len(set(keys))
