"""The Odds API name join -- the one unavoidable name join in the repo.
Fixtures only, no network."""

import pandas as pd

from espn_ff.odds import ids


def _props_df():
    return pd.DataFrame(
        [
            {"player_name": "Jayden Reed", "team": "Green Bay Packers"},
            {"player_name": "Terry McLaurin", "team": "Washington Commanders"},
            {"player_name": "Marvin Harrison Jr.", "team": "Arizona Cardinals"},
            {"player_name": "Some Rookie Nobody Has Heard Of", "team": "Kansas City Chiefs"},
        ]
    )


def test_resolve_matches_via_xwalk_first(monkeypatch):
    xwalk_df = pd.DataFrame(
        [{"display_name": "Jayden Reed", "latest_team": "GB", "espn_player_id": 4361741}]
    )
    out, unmatched = ids.resolve(_props_df(), xwalk_df=xwalk_df)
    row = out[out["player_name"] == "Jayden Reed"].iloc[0]
    assert row["espn_player_id"] == 4361741
    assert row["match_source"] == "xwalk"


def test_resolve_falls_back_to_sleeper_map_when_not_in_xwalk():
    espn_players_df = pd.DataFrame(
        [{"player_id": 7777, "player_name": "Terry McLaurin", "pro_team": "WSH"}]
    )
    sleeper_map_df = pd.DataFrame([{"sleeper_id": "999", "espn_player_id": 7777, "source": "espn_id"}])
    out, unmatched = ids.resolve(_props_df(), sleeper_map_df=sleeper_map_df, espn_players_df=espn_players_df)
    row = out[out["player_name"] == "Terry McLaurin"].iloc[0]
    assert row["espn_player_id"] == 7777
    assert row["match_source"] == "sleeper_map"


def test_resolve_falls_back_to_espn_pool_last():
    espn_players_df = pd.DataFrame(
        [{"player_id": 8888, "player_name": "Marvin Harrison Jr.", "pro_team": "ARI"}]
    )
    out, unmatched = ids.resolve(_props_df(), espn_players_df=espn_players_df)
    row = out[out["player_name"] == "Marvin Harrison Jr."].iloc[0]
    assert row["espn_player_id"] == 8888
    assert row["match_source"] == "espn_pool"


def test_resolve_keeps_unmatched_rows_rather_than_dropping_them():
    out, unmatched = ids.resolve(_props_df())
    assert len(out) == len(_props_df())  # nothing dropped
    unmatched_row = out.loc[out["player_name"] == "Some Rookie Nobody Has Heard Of"].iloc[0]
    assert unmatched_row["match_source"] == "unmatched"
    assert pd.isna(unmatched_row["espn_player_id"])
    assert len(unmatched) == len(out)  # none of the four fixtures resolve with no reference data at all


def test_resolve_bridges_full_team_name_to_espn_abbreviation():
    espn_players_df = pd.DataFrame(
        [{"player_id": 7777, "player_name": "Terry McLaurin", "pro_team": "WSH"}]
    )
    props = pd.DataFrame([{"player_name": "Terry McLaurin", "team": "Washington Commanders"}])
    out, unmatched = ids.resolve(props, espn_players_df=espn_players_df)
    assert out.iloc[0]["espn_player_id"] == 7777
    assert unmatched == []


def test_resolve_matches_on_name_alone_when_team_is_unknown():
    """The Odds API's game-level markets carry no team hint at all -- a
    unique name across the whole pool must still resolve with team=None."""
    espn_players_df = pd.DataFrame(
        [{"player_id": 4361741, "player_name": "Jayden Reed", "pro_team": "GB"}]
    )
    props = pd.DataFrame([{"player_name": "Jayden Reed", "team": None}])
    out, unmatched = ids.resolve(props, espn_players_df=espn_players_df)
    assert out.iloc[0]["espn_player_id"] == 4361741
    assert unmatched == []


def test_resolve_requires_team_to_break_a_tie_on_a_shared_name():
    espn_players_df = pd.DataFrame(
        [
            {"player_id": 1, "player_name": "Mike Williams", "pro_team": "NYJ"},
            {"player_id": 2, "player_name": "Mike Williams", "pro_team": "LAC"},
        ]
    )
    ambiguous = pd.DataFrame([{"player_name": "Mike Williams", "team": None}])
    out, unmatched = ids.resolve(ambiguous, espn_players_df=espn_players_df)
    assert unmatched == [0]  # no team hint -- can't safely pick either

    narrowed = pd.DataFrame([{"player_name": "Mike Williams", "team": "Los Angeles Chargers"}])
    out, unmatched = ids.resolve(narrowed, espn_players_df=espn_players_df)
    assert out.iloc[0]["espn_player_id"] == 2
    assert unmatched == []
