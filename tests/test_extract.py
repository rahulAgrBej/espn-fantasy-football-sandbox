"""Extractors run entirely off fixtures -- no network."""

import pandas as pd
import pytest

from espn_ff.extract import matchups, rosters, teams
from espn_ff.extract._common import team_name
from espn_ff.extract.players import players_frame


def test_players_frame_shape(player_pool):
    df = players_frame(player_pool, season=2026, week=1)
    assert len(df) == 5
    assert df["player_name"].notna().all()
    assert df["position"].isin({"QB", "RB", "WR", "TE", "K", "D/ST"}).all()
    assert (df["percent_owned"] > 0).all()


def test_players_frame_week_columns_are_optional(player_pool):
    assert "week_points" not in players_frame(player_pool, season=2026).columns
    assert "week_points" in players_frame(player_pool, season=2026, week=1).columns


def test_players_frame_uses_the_season_aware_selector(player_pool):
    """Josh Allen's naive week-1 row is 2025; the frame must show 2026."""
    df = players_frame(player_pool, season=2026, week=1).set_index("player_name")
    assert df.loc["Josh Allen", "week_points"] == pytest.approx(35.66)


@pytest.mark.parametrize(
    "team,expected",
    [
        ({"id": 1, "name": "Team Rocket"}, "Team Rocket"),
        ({"id": 2, "location": "Santa Cruz", "nickname": "Surfers"}, "Santa Cruz Surfers"),
        ({"id": 3, "name": "  "}, "Team 3"),
        ({"id": 4}, "Team 4"),
    ],
)
def test_team_name_handles_both_league_eras(team, expected):
    assert team_name(team) == expected


def test_teams_frame_matches_on_id_not_index():
    payload = {
        "members": [{"id": "{A}", "firstName": "Dana", "lastName": "Ray"}],
        "teams": [
            {"id": 7, "name": "Seven", "owners": ["{A}"],
             "record": {"overall": {"wins": 1, "losses": 0, "pointsFor": 101.5}}},
            {"id": 2, "name": "Two", "owners": [],
             "record": {"overall": {"wins": 0, "losses": 1, "pointsFor": 88.0}}},
        ],
    }
    df = teams.teams_frame(payload)
    assert list(df["team_id"]) == [2, 7]
    assert df.loc[df["team_id"] == 7, "owner"].iloc[0] == "Dana Ray"
    assert df.loc[df["team_id"] == 7, "points_for"].iloc[0] == 101.5



def test_rosters_frame_flags_starters():
    payload = {
        "teams": [
            {
                "id": 5,
                "name": "Five",
                "roster": {
                    "entries": [
                        _entry(1, "Starter QB", slot=0, position=1),
                        _entry(2, "Benched RB", slot=20, position=2),
                        _entry(3, "Hurt WR", slot=21, position=3),
                        _entry(4, "Flex RB", slot=23, position=2),
                    ]
                },
            }
        ]
    }
    df = rosters.rosters_frame(payload, season=2026, week=1)
    started = dict(zip(df["player_name"], df["started"]))
    assert started == {
        "Starter QB": True, "Flex RB": True, "Benched RB": False, "Hurt WR": False,
    }
    assert df.loc[df["started"], "points"].sum() == pytest.approx(30.0)
    assert set(df["lineup_slot"]) == {"QB", "Bench", "IR", "FLEX"}


def test_rosters_frame_empty_payload_is_empty_frame():
    assert rosters.rosters_frame({"teams": []}, season=2026, week=1).empty


def test_matchups_frame_pairs_both_sides_and_handles_byes():
    payload = {
        "teams": [{"id": 1, "name": "One"}, {"id": 2, "name": "Two"}, {"id": 3, "name": "Three"}],
        "schedule": [
            {"id": 10, "matchupPeriodId": 1, "winner": "HOME",
             "home": {"teamId": 1, "totalPoints": 120.0},
             "away": {"teamId": 2, "totalPoints": 99.5}},
            {"id": 11, "matchupPeriodId": 1, "winner": "UNDECIDED",
             "home": {"teamId": 3, "totalPoints": 80.0}},
        ],
    }
    df = matchups.matchups_frame(payload, season=2026)
    assert len(df) == 3  # two sides plus one bye
    one = df[df["team_id"] == 1].iloc[0]
    assert one["result"] == "W" and one["opponent_name"] == "Two"
    two = df[df["team_id"] == 2].iloc[0]
    assert two["result"] == "L"
    bye = df[df["team_id"] == 3].iloc[0]
    assert pd.isna(bye["opponent_id"]) or bye["opponent_id"] is None
    assert pd.isna(bye["result"])  # pandas normalises the None to NaN


def _entry(player_id, name, slot, position, points=15.0):
    return {
        "lineupSlotId": slot,
        "playerPoolEntry": {
            "player": {
                "id": player_id,
                "fullName": name,
                "defaultPositionId": position,
                "proTeamId": 12,
                "stats": [
                    {"seasonId": 2026, "scoringPeriodId": 1, "statSourceId": 0,
                     "statSplitTypeId": 1, "appliedTotal": points},
                    {"seasonId": 2025, "scoringPeriodId": 1, "statSourceId": 0,
                     "statSplitTypeId": 1, "appliedTotal": 999.0},
                ],
            }
        },
    }
