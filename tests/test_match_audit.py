"""
Tests for the matcher audit harness.

The negative control's headline — "100% of matches against a stale card are
false" — rests entirely on `same_pairing`. If that were trivially False the
number would be an artefact, so it is pinned in both directions here.
"""

from __future__ import annotations

from tools.match_audit import Decision, same_pairing, squad_mismatch


def _decision(fh, fa, mh, ma) -> Decision:
    return Decision(
        fixture_home=fh, fixture_away=fa, fixture_kickoff=None, tournament=None,
        matched_home=mh, matched_away=ma, matched_kickoff=None, event_id="sr:match:1",
        score=0.9, runner_up=0.0, margin=0.9, kickoff_delta_minutes=0.0,
        squad_mismatch=False,
    )


def test_same_pairing_accepts_a_genuine_match():
    """Must be able to return True, or the false-positive rate is meaningless."""
    assert same_pairing(_decision("Portsmouth", "Derby County", "Portsmouth FC", "Derby County"))


def test_same_pairing_accepts_noise_word_differences():
    assert same_pairing(_decision("Inter", "AS Roma", "Internazionale", "Roma"))


def test_same_pairing_rejects_a_different_club_in_the_same_city():
    """Gimnasia y Esgrima (La Plata) is not Gimnasia y Esgrima Mendoza."""
    assert not same_pairing(
        _decision("Gimnasia y Esgrima", "Boca Juniors", "Gimnasia y Esgrima Mendoza", "Boca Juniors")
    )


def test_same_pairing_rejects_a_wrong_opponent():
    assert not same_pairing(_decision("Lazio", "Mantova", "Lazio", "AC Milan"))


def test_same_pairing_rejects_an_unmatched_decision():
    assert not same_pairing(_decision("Lazio", "Mantova", None, None))


def test_squad_mismatch_flags_reserve_and_youth_sides():
    assert squad_mismatch("Atletico Madrid", "Atletico Madrid B")
    assert squad_mismatch("Boca Juniors", "Boca Juniors Reserves")
    assert squad_mismatch("West Ham United", "West Ham United Women")
    assert squad_mismatch("Marconi Stallions U20", "Marconi Stallions")


def test_squad_mismatch_allows_two_first_teams():
    assert not squad_mismatch("Portsmouth", "Portsmouth FC")
    assert not squad_mismatch("Real Madrid", "Real Madrid")


def test_squad_mismatch_allows_two_reserve_sides():
    assert not squad_mismatch("Atletico Madrid B", "Atletico Madrid B")
