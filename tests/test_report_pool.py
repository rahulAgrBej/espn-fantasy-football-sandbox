"""Free-agent anti-join arithmetic -- fixtures only, no network."""

import pandas as pd

from espn_ff.report import pool


def _pool_df():
    return pd.DataFrame(
        [
            {"player_id": 1, "player_name": "Rostered Guy", "pro_team": "NYG", "week": 2, "on_team_id": 0},
            {"player_id": 2, "player_name": "Free Agent Guy", "pro_team": "NYG", "week": 2, "on_team_id": 0},
            {"player_id": 3, "player_name": "Other Team Rostered", "pro_team": "LAR", "week": 2, "on_team_id": 0},
            {"player_id": 4, "player_name": "Prior Week Only", "pro_team": "LAR", "week": 1, "on_team_id": 0},
        ]
    )


def _rosters_df():
    return pd.DataFrame(
        [
            {"player_id": 1, "week": 2, "team_id": 5},
            {"player_id": 3, "week": 2, "team_id": 7},
        ]
    )


def test_free_agents_excludes_rostered_players():
    result = pool.free_agents(2, pool_df=_pool_df(), rosters_df=_rosters_df())
    assert set(result["player_id"]) == {2}


def test_a_rostered_player_never_appears_as_a_free_agent():
    result = pool.free_agents(2, pool_df=_pool_df(), rosters_df=_rosters_df())
    assert 1 not in set(result["player_id"])
    assert 3 not in set(result["player_id"])


def test_free_agents_filters_to_the_requested_week():
    result = pool.free_agents(1, pool_df=_pool_df(), rosters_df=_rosters_df())
    assert set(result["player_id"]) == {4}


def test_on_team_id_is_not_consulted():
    """Every row in player-pool.csv carries on_team_id == 0 -- the anti-join
    against weekly-rosters.csv is the only thing deciding ownership."""
    pool_df = _pool_df()
    assert (pool_df["on_team_id"] == 0).all()
    result = pool.free_agents(2, pool_df=pool_df, rosters_df=_rosters_df())
    # Rostered players (1, 3) are still excluded despite on_team_id == 0
    # matching every row including theirs.
    assert set(result["player_id"]) == {2}


def test_free_agents_with_no_rosters_export_treats_nobody_as_rostered():
    result = pool.free_agents(2, pool_df=_pool_df(), rosters_df=pd.DataFrame())
    assert set(result["player_id"]) == {1, 2, 3}


def test_free_agents_returns_empty_frame_when_pool_is_empty():
    result = pool.free_agents(2, pool_df=pd.DataFrame(), rosters_df=_rosters_df())
    assert result.empty
