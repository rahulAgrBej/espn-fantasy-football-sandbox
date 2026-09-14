"""Sleeper <-> ESPN identity resolution -- fixtures only, no network."""

import json
from pathlib import Path

import pandas as pd
import pytest

from espn_ff.sleeper import ids, snapshots

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def sleeper_raw():
    return json.loads((FIXTURES / "sleeper_players_small.json").read_text())


@pytest.fixture
def sleeper_df(sleeper_raw):
    return snapshots.slim_players(sleeper_raw, fetched_at=1000.0)


@pytest.fixture
def espn_players_df():
    return pd.DataFrame(
        [
            {"player_id": 4361741, "player_name": "Jayden Reed", "position": "WR", "pro_team": "GB"},
            {"player_id": 8888, "player_name": "Marvin Harrison Jr.", "position": "WR", "pro_team": "ARI"},
            {"player_id": 7777, "player_name": "Terry McLaurin", "position": "WR", "pro_team": "WSH"},
            {"player_id": -16033, "player_name": "49ers D/ST", "position": "D/ST", "pro_team": "SF"},
        ]
    )


def test_normalize_name_strips_punctuation_and_suffix():
    assert ids.normalize_name("Marvin Harrison Jr.") == "marvin harrison"
    assert ids.normalize_name("D'Andre Swift") == "dandre swift"


def test_normalize_team_maps_sleeper_alias_to_espn():
    assert ids.normalize_team("WAS") == "WSH"
    assert ids.normalize_team("JAC") == "JAX"
    assert ids.normalize_team("LA") == "LAR"
    assert ids.normalize_team("GB") == "GB"


def test_normalize_position_maps_def_to_dst():
    assert ids.normalize_position("DEF") == "D/ST"
    assert ids.normalize_position("WR") == "WR"


def test_resolve_prefers_espn_id_when_present(sleeper_df, espn_players_df):
    map_df, unmatched = ids.resolve(sleeper_df, espn_players_df)
    row = map_df[map_df["sleeper_id"] == "1001"].iloc[0]
    assert row["espn_player_id"] == 4361741
    assert row["source"] == "espn_id"


def test_resolve_falls_back_to_name_path_when_espn_id_missing(sleeper_df, espn_players_df):
    map_df, unmatched = ids.resolve(sleeper_df, espn_players_df)
    row = map_df[map_df["sleeper_id"] == "1002"].iloc[0]
    assert row["espn_player_id"] == 8888
    assert row["source"] == "name"


def test_resolve_bridges_was_wsh_team_alias(sleeper_df, espn_players_df):
    map_df, unmatched = ids.resolve(sleeper_df, espn_players_df)
    row = map_df[map_df["sleeper_id"] == "1003"].iloc[0]
    assert row["espn_player_id"] == 7777
    assert row["source"] == "name"


def test_resolve_bridges_def_dst_position_alias_by_team_not_name(sleeper_df, espn_players_df):
    """Sleeper's DEF full_name ('San Francisco 49ers') and ESPN's D/ST name
    ('49ers D/ST') share no common tokens -- this only matches because the
    D/ST path resolves on team + position, skipping the name comparison."""
    map_df, unmatched = ids.resolve(sleeper_df, espn_players_df)
    row = map_df[map_df["sleeper_id"] == "1004"].iloc[0]
    assert row["espn_player_id"] == -16033
    assert row["source"] == "name"


def test_resolve_logs_unmatched_instead_of_dropping(sleeper_df, espn_players_df):
    map_df, unmatched = ids.resolve(sleeper_df, espn_players_df)
    assert "1006" in unmatched
    assert "1006" not in set(map_df["sleeper_id"])


def test_resolve_preserves_manual_rows_across_a_rerun(sleeper_df, espn_players_df):
    existing = pd.DataFrame(
        [{"sleeper_id": "1001", "espn_player_id": 424242, "source": "manual"}]
    )
    map_df, unmatched = ids.resolve(sleeper_df, espn_players_df, existing_map=existing)
    row = map_df[map_df["sleeper_id"] == "1001"].iloc[0]
    assert row["espn_player_id"] == 424242
    assert row["source"] == "manual"


def test_missing_from_roster_flags_unresolved_players(sleeper_df, espn_players_df):
    map_df, _ = ids.resolve(sleeper_df, espn_players_df)
    missing = ids.missing_from_roster(map_df, [4361741, 7777, 99999999])
    assert missing == [99999999]
