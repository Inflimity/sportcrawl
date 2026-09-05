"""
Regression tests for the form ticket ignoring competition quality.

Ticket 4 arrived day after day as a card of minor leagues, and two independent
defects caused it.

**The allowlist never applied.** ``run_pipeline`` and ``run_dual_pipeline`` both
called ``filter_fixtures(..., allow_unlisted=True)`` as a hardcoded literal, so
``ALLOWED_COMPETITIONS`` was bypassed for every ticket ever booked and only the
exclusion patterns ran. On the stored card that left 2,163 fixtures of which 77%
were Tier 3, including Bolivian regional divisions and Argentine Primera C.
Enforcing the allowlist cuts it to 811 and lifts Tier 1+2 from 23% to 59%.

**The form path had no tier at all.** ``screen.py`` has always sorted
``(tier, -conviction)``, but ``form_pick._build_pick`` never passed ``tier`` to
``Pick``, so every form candidate carried the default of 3 and both form
screeners ranked on probability alone. With 77% of the pool in the lowest tier,
truncating that ranking to the ten priced fixtures lands on small leagues by
weight of numbers whatever else is on the card.
"""

from core.predictor.enrich import TeamForm
from core.predictor.filter import Fixture
from core.predictor.form_pick import screen_form, screen_form_candidates
from core.predictor.leagues import competition_tier

ELITE = ("England", "Premier League")
MAJOR = ("Netherlands", "Eredivisie")
MINOR = ("Bolivia", "Primera B Cochabamba")


def _form(tid, name, results, over15=0.8, used=None):
    return TeamForm(
        team_id=tid, name=name, recent_results=results,
        btts_rate=0.5, over25_rate=0.5, over15_rate=over15,
        matches_used=used if used is not None else len(results),
    )


def _fixture(mid, category, tournament):
    return Fixture(
        match_id=mid, tournament=tournament, category=category,
        home_name=f"H{mid}", away_name=f"A{mid}",
        home_id=mid * 2, away_id=mid * 2 + 1,
        start_utc="2026-09-05T18:00:00+00:00", start_local="2026-09-05 19:00",
    )


def _card():
    """One fixture per tier. The minor-league game has the strongest form."""
    fixtures, forms = [], {}
    for mid, (cat, tourn), over15 in (
        (1, ELITE, 0.60),
        (2, MAJOR, 0.70),
        (3, MINOR, 0.99),   # best read on the card, worst competition
    ):
        fx = _fixture(mid, cat, tourn)
        fixtures.append(fx)
        forms[fx.home_id] = _form(fx.home_id, fx.home_name, "WWWWW", over15=over15)
        forms[fx.away_id] = _form(fx.away_id, fx.away_name, "WWWWW", over15=over15)
    return fixtures, forms


class TestTierReachesTheFormPick:
    def test_form_picks_carry_their_competition_tier(self):
        """Every form pick used to carry Pick's default of 3, elite included."""
        fixtures, forms = _card()
        picks = screen_form(fixtures, forms, min_matches=4)
        by_fixture = {p.fixture.match_id: p.tier for p in picks}
        assert by_fixture[1] == competition_tier(*ELITE) == 1
        assert by_fixture[2] == competition_tier(*MAJOR) == 2
        assert by_fixture[3] == competition_tier(*MINOR) == 3


class TestOrdering:
    def test_major_competitions_lead_the_form_screen(self):
        fixtures, forms = _card()
        picks = screen_form(fixtures, forms, min_matches=4)
        assert [p.tier for p in picks] == sorted(p.tier for p in picks)
        assert picks[0].fixture.category == ELITE[0]

    def test_the_minor_league_still_wins_on_form_alone(self):
        """
        The trade-off, stated as a test: tier priority puts a weaker read in a
        major league ahead of a stronger one in a minor league. With the flag
        off, the strongest read leads — the previous behaviour.
        """
        fixtures, forms = _card()
        picks = screen_form(fixtures, forms, min_matches=4, tier_priority=False)
        assert picks[0].fixture.category == MINOR[0]

    def test_candidates_are_tier_ordered_before_the_fixture_limit_bites(self):
        """
        ``fixture_limit`` is what decides the ticket's whole universe — pricing
        costs one SportyBet call per fixture, so only the head of the list is
        ever bookable. Ordering must therefore happen before the truncation.
        """
        fixtures, forms = _card()
        candidates = screen_form_candidates(
            fixtures, forms, min_matches=4, per_fixture=1, fixture_limit=1
        )
        assert len(candidates) == 1
        assert candidates[0].fixture.category == ELITE[0]

    def test_a_tier_ceiling_drops_lesser_competitions_entirely(self):
        fixtures, forms = _card()
        picks = screen_form(fixtures, forms, min_matches=4, max_tier=2)
        assert {p.tier for p in picks} == {1, 2}
        assert all(p.fixture.category != MINOR[0] for p in picks)


class TestAllowlistIsEnforced:
    def test_the_pipeline_no_longer_hardcodes_the_bypass(self):
        """
        The literal `allow_unlisted=True` is what made the allowlist dead code
        for every booked ticket. Pin its absence: a future edit reinstating it
        would restore the bug silently.
        """
        source = open("services/pipeline.py").read()
        assert "allow_unlisted=True" not in source
        assert source.count("allow_unlisted=not require_allowlisted_leagues") == 2
