"""Live checks against the real league. Skipped without credentials.

These cross-check our parsing against numbers ESPN computed itself, which is
the strongest available evidence that the stat selector and the starter/bench
classification are right. No league data is committed -- everything is fetched
(or served from data/raw/) at run time.

    .venv/bin/pytest -m integration
"""

import pytest

from espn_ff import config
from espn_ff.client import EspnClient
from espn_ff.extract import teams
from espn_ff.extract.rosters import rosters_frame

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not config.has_credentials(), reason="ESPN_S2/SWID not set"),
]


@pytest.fixture(scope="module")
def client():
    return EspnClient()


@pytest.fixture(scope="module")
def week(client):
    return client.current_scoring_period()


@pytest.fixture(scope="module")
def payload(client, week):
    return client.get_league(["mRoster", "mTeam", "mMatchupScore"], scoring_period=week)


def test_starter_totals_match_espns_own_arithmetic(payload, week):
    """Our summed starters must equal roster.appliedStatTotal, team by team.

    This is the end-to-end proof of the stat selector: if it picked up a wrong
    season, a projection, or a season aggregate, these totals would diverge.
    """
    df = rosters_frame(payload, season=config.SEASON, week=week)
    espn = {t["id"]: (t.get("roster") or {}).get("appliedStatTotal") for t in payload["teams"]}

    mismatches = {}
    for team_id, group in df.groupby("team_id"):
        ours = round(group.loc[group["started"], "points"].sum(), 2)
        theirs = round(espn.get(team_id) or 0.0, 2)
        if ours != theirs:
            mismatches[team_id] = (ours, theirs)

    assert not mismatches, f"starter totals diverge from ESPN: {mismatches}"


def test_starter_totals_match_the_matchup_scoreboard(payload, week):
    """Second independent source: the scoreboard's live/final team points."""
    df = rosters_frame(payload, season=config.SEASON, week=week)
    board = {}
    for game in payload.get("schedule") or []:
        if game.get("matchupPeriodId") != week:
            continue
        for side in ("home", "away"):
            entry = game.get(side) or {}
            if entry.get("teamId"):
                board[entry["teamId"]] = entry.get("totalPointsLive") or entry.get("totalPoints")

    assert board, "no matchups found for the current week"
    for team_id, group in df.groupby("team_id"):
        if team_id not in board:
            continue
        ours = round(group.loc[group["started"], "points"].sum(), 2)
        assert ours == pytest.approx(round(board[team_id], 2)), f"team {team_id}"


def test_every_team_has_a_full_roster(payload, week):
    df = rosters_frame(payload, season=config.SEASON, week=week)
    counts = df.groupby("team_id").size()
    assert len(counts) > 1
    assert counts.min() > 0
    assert df["player_name"].notna().all()


def test_teams_frame_resolves_every_owner(payload):
    df = teams.teams_frame(payload)
    assert len(df) == len(payload["teams"])
    assert df["team_name"].notna().all()
    # Owner GUIDs must resolve to real names, not fall through to the raw guid.
    assert not df["owner"].str.startswith("{").any()
