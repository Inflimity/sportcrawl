"""
Regression tests for the booker resolving a leg to the wrong game.

Two independent defects put a different fixture on the betslip from the one the
engine screened, and both are pinned here.

**Whole-string similarity invented evidence.** ``difflib`` over full names
rewards a shared prefix, which is precisely what two clubs from one city have.
It scored ``Cagliari`` against ``Real Madrid`` at 0.526 — clear of the 0.48
threshold every caller books at — on coincidental letters alone. Meanwhile
"united" sat in ``NOISE_TOKENS`` and was deleted before comparison, collapsing
``Man Utd`` into ``Man City`` and ``Sheffield United`` into ``Sheffield
Wednesday`` at a flat 0.85, a score no usable threshold sits above.

**Nothing looked at the clock.** The card-wide booking window closed
wrong-*day* matches, but two games on the same day sharing a club name were
still separated by names alone. The fixture's own kickoff is the check that
measured 100% against provably-wrong matches over 431 fixtures.
"""

import pytest

from core.prediction_parser import parse_prediction_line
from core.team_matcher import fixture_key, match_fixture, team_similarity

BOOKING_THRESHOLD = 0.48


def event(event_id, home, away, kickoff_ms=None):
    ev = {"eventId": event_id, "homeTeamName": home, "awayTeamName": away}
    if kickoff_ms is not None:
        ev["estimateStartTime"] = kickoff_ms
    return ev


class TestNamesThatMustNotMatch:
    """Pairs that scored above the booking threshold and are different teams."""

    @pytest.mark.parametrize(
        "a, b",
        [
            ("Cagliari", "Real Madrid"),          # scored 0.526 on shared letters
            ("Man Utd", "Man City"),              # both reduced to "manchester"
            ("Manchester United", "Manchester City"),
            ("Sheffield United", "Sheffield Wednesday"),
            ("Newcastle United", "Newcastle Jets"),
            ("Atletico Madrid", "Atletico Madrid B"),
            ("West Ham United", "West Ham United Women"),
        ],
    )
    def test_scores_below_the_booking_threshold(self, a, b):
        assert team_similarity(a, b) < BOOKING_THRESHOLD

    def test_a_squad_marker_on_one_side_only_is_disqualifying(self):
        """A reserve or women's side is a different team at any similarity."""
        assert team_similarity("Atletico Madrid", "Atletico Madrid B") == 0.0
        assert team_similarity("Barcelona", "Barcelona Women") == 0.0

    def test_sharing_no_word_is_no_evidence(self):
        assert team_similarity("Cagliari", "Real Madrid") == 0.0


class TestNamesThatMustStillMatch:
    """The recall side. Raising precision must not stop real fixtures booking."""

    @pytest.mark.parametrize(
        "a, b",
        [
            ("Inter", "Internazionale"),
            ("Leeds United", "Leeds"),
            ("Nottingham Forest", "Nottingham"),
            ("Bayern Munich", "Bayern München"),
            ("Paris Saint-Germain", "PSG"),
            ("Brighton", "Brighton & Hove Albion"),
            ("Wolverhampton Wanderers", "Wolves"),
            ("Tottenham Hotspur", "Spurs"),
            ("1. FC Koln", "FC Koln"),
            ("Borussia Monchengladbach", "M'gladbach"),
            ("Real Madrid", "Real Madrid"),
        ],
    )
    def test_scores_above_the_booking_threshold(self, a, b):
        assert team_similarity(a, b) >= BOOKING_THRESHOLD


class TestKickoffSeparatesSameDayGames:
    def test_the_wrong_opponent_is_no_longer_reachable(self):
        """
        `Cagliari v Inter` used to resolve to `Real Madrid v Internazionale`:
        one correct side carrying a completely wrong opponent.
        """
        card = [event("C", "Real Madrid", "Internazionale", 1788000000000)]
        assert match_fixture("Cagliari", "Inter", card, threshold=BOOKING_THRESHOLD) is None

    def test_kickoff_picks_between_two_events_sharing_a_club_name(self):
        card = [
            event("A", "Manchester City", "Arsenal", 1788000000000),
            event("B", "Manchester United", "Chelsea", 1788010800000),
        ]
        matched = match_fixture(
            "Man Utd", "Chelsea", card,
            threshold=BOOKING_THRESHOLD, kickoff=1788010800000,
        )
        assert matched is not None and matched[0]["eventId"] == "B"

    def test_an_event_outside_the_tolerance_is_refused(self):
        card = [event("B", "Manchester United", "Chelsea", 1788010800000)]
        assert match_fixture(
            "Manchester United", "Chelsea", card,
            threshold=BOOKING_THRESHOLD, kickoff="2026-09-05T10:00:00",
        ) is None

    def test_iso_and_epoch_kickoffs_are_read_the_same_way(self):
        card = [event("B", "Manchester United", "Chelsea", 1788010800000)]
        by_epoch = match_fixture(
            "Manchester United", "Chelsea", card,
            threshold=BOOKING_THRESHOLD, kickoff=1788010800000,
        )
        by_iso = match_fixture(
            "Manchester United", "Chelsea", card,
            threshold=BOOKING_THRESHOLD, kickoff="2026-08-29T13:40:00+00:00",
        )
        assert by_epoch is not None
        assert by_iso is not None and by_iso[0]["eventId"] == by_epoch[0]["eventId"]

    def test_an_event_publishing_no_kickoff_stays_eligible(self):
        """A field rename at SportyBet must degrade to name matching, not an empty card."""
        card = [event("B", "Manchester United", "Chelsea")]
        assert match_fixture(
            "Manchester United", "Chelsea", card,
            threshold=BOOKING_THRESHOLD, kickoff=1788010800000,
        ) is not None

    def test_no_kickoff_supplied_leaves_behaviour_unchanged(self):
        card = [event("B", "Manchester United", "Chelsea", 1788010800000)]
        assert match_fixture(
            "Manchester United", "Chelsea", card, threshold=BOOKING_THRESHOLD
        ) is not None


class TestKickoffSurvivesTheTextContract:
    def test_the_side_table_key_matches_what_the_parser_produces(self):
        """
        Predictions reach the booker as text, which has no room for a kickoff,
        so it travels in a dict keyed by fixture. If the key the pipeline writes
        and the key the booker reads ever diverge, the kickoff is silently
        dropped and the wrong-game bug returns without a single test failing.
        """
        line = "Manchester United vs Chelsea - Over 1.5"
        bet = parse_prediction_line(line)
        assert bet is not None
        written = fixture_key("Manchester United", "Chelsea")
        assert fixture_key(bet.home_team, bet.away_team) == written

    def test_parsed_bets_carry_the_kickoff_the_pipeline_supplies(self):
        bet = parse_prediction_line("Arsenal vs Chelsea - GG")
        assert bet is not None and bet.kickoff is None
        bet.kickoff = "2026-08-29T13:40:00+00:00"
        assert bet.to_dict()["kickoff"] == "2026-08-29T13:40:00+00:00"
