"""
Measure the fixture -> SportyBet event matcher.

Why this exists
---------------
``core.team_matcher.match_fixture`` decides which SportyBet event a screened
fixture is booked against. It compares team names only, at ``threshold=0.48``,
and until now nothing had ever measured it. A 2-odds banker shipped a leg for a
match kicking off the following night, which is what prompted this.

Two modes, because they answer different questions and cost different things.

``--negative-control``
    Take a *past* day's fixtures (a saved SofaScore export) and match them
    against the *current* SportyBet card. Those games were played days ago, so
    with the rare exception of a genuine rematch **every match returned is a
    false positive**. Ground truth for free, no labelling, runnable today. It
    measures precision: of the matches the engine makes, how many are wrong.

``--log`` / ``--report``
    The forward-collection mode. SportyBet publishes no historical card — the
    same wall that makes historical prices unrecoverable — so the matcher
    cannot be backtested against real same-day data. Instead every live run
    appends its decisions to JSONL: the chosen event, its score, the runner-up's
    score, and the gap between the fixture's kickoff and the event's. Kickoff
    agreement is a free correctness label (<=15 min right, >6 h wrong), so after
    a few days of runs ``--report`` can sweep the threshold over real decisions.

Deliberately does NOT modify the matcher. Measure first, then change.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.team_matcher import normalize_team_name, team_similarity  # noqa: E402

SPORTYBET_CARD = (
    "https://www.sportybet.com/api/ng/factsCenter/pcUpcomingEvents"
    "?sportId=sr%3Asport%3A1&marketId=1%2C18%2C10%2C29&pageSize=100&pageNum={page}&_t={ts}"
)

# Squad-level markers. If exactly one side carries one, the two names denote
# different teams however similar the characters are — "Atletico Madrid" and
# "Atletico Madrid B" score 0.85 today.
SQUAD_MARKERS = {
    "b", "ii", "iii", "reserve", "reserves", "res", "u17", "u18", "u19", "u20",
    "u21", "u23", "women", "ladies", "fem", "feminine", "academy", "youth", "jr",
}


@dataclass
class Decision:
    """One matcher decision, with everything needed to grade it later."""

    fixture_home: str
    fixture_away: str
    fixture_kickoff: Optional[str]
    tournament: Optional[str]
    matched_home: Optional[str]
    matched_away: Optional[str]
    matched_kickoff: Optional[str]
    event_id: Optional[str]
    score: float
    runner_up: float
    margin: float
    kickoff_delta_minutes: Optional[float]
    squad_mismatch: bool


def squad_tokens(name: str) -> set[str]:
    return {t for t in normalize_team_name(name).split() if t in SQUAD_MARKERS}


def squad_mismatch(a: str, b: str) -> bool:
    """True when one name is a reserve/women/youth side and the other is not."""
    return squad_tokens(a) != squad_tokens(b)


@lru_cache(maxsize=200_000)
def _sim(a: str, b: str) -> float:
    """Cached team_similarity. The sweep asks the same pair at every threshold."""
    return team_similarity(a, b)


def score_pair(home: str, away: str, event: dict[str, Any]) -> tuple[float, float, float]:
    """Return (combined, home_score, away_score) exactly as match_fixture does."""
    c_home = event.get("homeTeamName") or event.get("home_team") or event.get("home", "")
    c_away = event.get("awayTeamName") or event.get("away_team") or event.get("away", "")
    sh = _sim(home, c_home)
    sa = _sim(away, c_away)
    return (sh + sa) / 2.0, sh, sa


def shortlist(
    home: str, away: str, events: list[dict[str, Any]], floor: float
) -> list[tuple[float, float, float, dict[str, Any]]]:
    """
    Score a fixture against the whole card once, keeping anything that could
    matter at any threshold at or above ``floor``. The sweep then filters this
    cached list instead of rescoring 500k pairs per threshold.
    """
    out = []
    for ev in events:
        combined, sh, sa = score_pair(home, away, ev)
        if sh >= floor and sa >= floor:
            out.append((combined, sh, sa, ev))
    out.sort(key=lambda item: item[0], reverse=True)
    return out


def rank_candidates(
    shortlisted: list[tuple[float, float, float, dict[str, Any]]], threshold: float
) -> list[tuple[float, dict[str, Any]]]:
    """Filter a cached shortlist to one threshold, best first."""
    return [(c, ev) for c, sh, sa, ev in shortlisted if sh >= threshold and sa >= threshold]


def event_kickoff(event: dict[str, Any]) -> Optional[datetime]:
    raw = event.get("estimateStartTime")
    if raw in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def decide(
    fixture: dict[str, Any],
    shortlisted: list[tuple[float, float, float, dict[str, Any]]],
    threshold: float,
) -> Decision:
    home, away = fixture["home_team"], fixture["away_team"]
    ranked = rank_candidates(shortlisted, threshold)
    ko = fixture.get("_kickoff")

    if not ranked:
        return Decision(
            home, away, ko.isoformat() if ko else None, fixture.get("tournament"),
            None, None, None, None, 0.0, 0.0, 0.0, None, False,
        )

    best_score, best = ranked[0]
    runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
    ev_ko = event_kickoff(best)
    delta = (ev_ko - ko).total_seconds() / 60.0 if (ev_ko and ko) else None

    return Decision(
        fixture_home=home,
        fixture_away=away,
        fixture_kickoff=ko.isoformat() if ko else None,
        tournament=fixture.get("tournament"),
        matched_home=best.get("homeTeamName"),
        matched_away=best.get("awayTeamName"),
        matched_kickoff=ev_ko.isoformat() if ev_ko else None,
        event_id=best.get("eventId"),
        score=round(best_score, 4),
        runner_up=round(runner_up, 4),
        margin=round(best_score - runner_up, 4),
        kickoff_delta_minutes=round(delta, 1) if delta is not None else None,
        squad_mismatch=(
            squad_mismatch(home, best.get("homeTeamName") or "")
            or squad_mismatch(away, best.get("awayTeamName") or "")
        ),
    )


def same_pairing(d: Decision) -> bool:
    """
    Strict, non-circular check that the matched event is the same fixture.

    Uses normalized token-set equality rather than the similarity score being
    audited, so a generous score cannot vouch for itself.
    """
    if not d.matched_home or not d.matched_away:
        return False
    return (
        set(normalize_team_name(d.fixture_home).split())
        == set(normalize_team_name(d.matched_home).split())
        and set(normalize_team_name(d.fixture_away).split())
        == set(normalize_team_name(d.matched_away).split())
    )


def fetch_card(max_pages: int = 12) -> list[dict[str, Any]]:
    import httpx

    ts = int(time.time() * 1000)
    events: list[dict[str, Any]] = []
    with httpx.Client(headers={"User-Agent": "Mozilla/5.0"}, timeout=25) as client:
        for page in range(1, max_pages + 1):
            resp = client.get(SPORTYBET_CARD.format(page=page, ts=ts))
            if resp.status_code != 200:
                break
            tournaments = (resp.json().get("data") or {}).get("tournaments") or []
            if not tournaments:
                break
            for tour in tournaments:
                for ev in tour.get("events", []):
                    ev["_tournament"] = tour.get("name")
                    events.append(ev)
    return events


def _team_name(value: Any) -> str:
    """Exports carry teams as {"id","name",...}; the DB path carries plain strings."""
    if isinstance(value, dict):
        return value.get("name") or ""
    return value or ""


def load_fixtures(paths: Iterable[str]) -> list[dict[str, Any]]:
    fixtures: list[dict[str, Any]] = []
    for path in paths:
        blob = json.loads(Path(path).read_text())
        for m in blob.get("matches", blob if isinstance(blob, list) else []):
            m["home_team"] = _team_name(m.get("home_team"))
            m["away_team"] = _team_name(m.get("away_team"))
            if not m["home_team"] or not m["away_team"]:
                continue
            raw = m.get("start_time_utc")
            try:
                ko = datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None
                m["_kickoff"] = ko.replace(tzinfo=timezone.utc) if (ko and ko.tzinfo is None) else ko
            except (AttributeError, ValueError):
                m["_kickoff"] = None
            fixtures.append(m)
    return fixtures


def negative_control(fixtures, events, thresholds, show: int = 12) -> None:
    print(f"\nFixtures: {len(fixtures)}   Live SportyBet events: {len(events)}")
    print("Every match below is a FALSE POSITIVE unless the same two teams meet again.\n")
    print(f"{'thresh':>7}{'matched':>9}{'false+':>8}{'rate':>8}{'squad-mix':>11}{'margin<0.05':>13}")
    print("-" * 56)

    floor = min(thresholds)
    cache = [(f, shortlist(f["home_team"], f["away_team"], events, floor)) for f in fixtures]

    baseline: list[Decision] = []
    for t in thresholds:
        decisions = [decide(f, sl, t) for f, sl in cache]
        made = [d for d in decisions if d.event_id]
        false_pos = [d for d in made if not same_pairing(d)]
        squad = [d for d in made if d.squad_mismatch]
        thin = [d for d in made if 0 < d.margin < 0.05]
        rate = len(false_pos) / len(made) if made else 0.0
        print(
            f"{t:>7.2f}{len(made):>9}{len(false_pos):>8}{rate:>7.0%}"
            f"{len(squad):>11}{len(thin):>13}"
        )
        if abs(t - 0.48) < 1e-9:
            baseline = false_pos

    if baseline:
        print(f"\nWorst false positives at the live threshold (0.48), by score:\n")
        for d in sorted(baseline, key=lambda x: -x.score)[:show]:
            delta = (
                f"{d.kickoff_delta_minutes / 1440:+.1f}d"
                if d.kickoff_delta_minutes is not None
                else "n/a"
            )
            flag = " [SQUAD]" if d.squad_mismatch else ""
            print(f"  {d.score:.2f} (margin {d.margin:.2f}, kickoff {delta}){flag}")
            print(f"        fixture: {d.fixture_home} vs {d.fixture_away}")
            print(f"        matched: {d.matched_home} vs {d.matched_away}")


def report(path: str, thresholds) -> None:
    rows = [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]
    graded = [r for r in rows if r.get("kickoff_delta_minutes") is not None]
    if not graded:
        print(f"{len(rows)} decisions logged, none with both kickoffs — nothing to grade yet.")
        return

    print(f"\n{len(rows)} decisions logged, {len(graded)} gradable by kickoff agreement.\n")
    print(f"{'thresh':>7}{'matched':>9}{'correct':>9}{'wrong':>7}{'precision':>11}{'coverage':>10}")
    print("-" * 53)
    total = len(rows)
    for t in thresholds:
        kept = [r for r in graded if r["score"] >= t]
        right = [r for r in kept if abs(r["kickoff_delta_minutes"]) <= 15]
        wrong = [r for r in kept if abs(r["kickoff_delta_minutes"]) > 360]
        prec = len(right) / len(kept) if kept else 0.0
        print(
            f"{t:>7.2f}{len(kept):>9}{len(right):>9}{len(wrong):>7}"
            f"{prec:>10.0%}{len(kept) / total:>10.0%}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fixtures", nargs="*", default=[], help="SofaScore JSON export(s)")
    ap.add_argument("--negative-control", action="store_true", help="grade past fixtures against the live card")
    ap.add_argument("--log", metavar="JSONL", help="append decisions for forward collection")
    ap.add_argument("--report", metavar="JSONL", help="sweep the threshold over a decision log")
    ap.add_argument("--card", help="cached card JSON (defaults to fetching live)")
    ap.add_argument("--min", type=float, default=0.40)
    ap.add_argument("--max", type=float, default=0.90)
    ap.add_argument("--step", type=float, default=0.05)
    args = ap.parse_args()

    thresholds = []
    t = args.min
    while t <= args.max + 1e-9:
        thresholds.append(round(t, 2))
        t += args.step

    if args.report:
        report(args.report, thresholds)
        return

    fixtures = load_fixtures(args.fixtures)
    if not fixtures:
        ap.error("--fixtures is required unless using --report")

    events = json.loads(Path(args.card).read_text()) if args.card else fetch_card()
    if not events:
        ap.error("no SportyBet events available")

    if args.log:
        with open(args.log, "a") as fh:
            for f in fixtures:
                sl = shortlist(f["home_team"], f["away_team"], events, 0.48)
                fh.write(json.dumps(asdict(decide(f, sl, 0.48))) + "\n")
        print(f"Appended {len(fixtures)} decisions to {args.log}")

    if args.negative_control or not args.log:
        negative_control(fixtures, events, thresholds)


if __name__ == "__main__":
    main()
