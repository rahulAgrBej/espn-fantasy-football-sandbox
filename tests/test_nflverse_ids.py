"""ESPN <-> nflverse identity resolution -- fixtures only, no network."""

import pandas as pd

from espn_ff.nflverse import features, ids


def test_build_xwalk_resolves_espn_id_from_players_parquet(nflverse_players_df):
    xwalk, stats = ids.build_xwalk(nflverse_players_df)
    row = xwalk[xwalk["gsis_id"] == "00-0001"].iloc[0]
    assert row["espn_player_id"] == 4361741
    assert row["source"] == "players"
    assert stats["from_players"] == len(xwalk)  # no fallback given -- nothing backfilled
    assert stats["from_db_playerids"] == 0


def test_build_xwalk_casts_espn_id_string_to_int(nflverse_players_df):
    xwalk, _ = ids.build_xwalk(nflverse_players_df)
    row = xwalk[xwalk["gsis_id"] == "00-0001"].iloc[0]
    assert isinstance(row["espn_player_id"], int) or row["espn_player_id"] == 4361741
    assert str(xwalk["espn_player_id"].dtype) == "Int64"


def test_build_xwalk_fallback_fills_a_null_espn_id(nflverse_players_df, nflverse_db_playerids_df):
    xwalk, stats = ids.build_xwalk(nflverse_players_df, nflverse_db_playerids_df)
    row = xwalk[xwalk["gsis_id"] == "00-0005"].iloc[0]
    assert row["espn_player_id"] == 4444444
    assert row["source"] == "db_playerids"
    assert stats["from_db_playerids"] == 1


def test_build_xwalk_keeps_unresolved_rows_without_dropping(nflverse_players_df):
    """No fallback given -- Marvin Harrison's null espn_id has nowhere to
    resolve from. The row must still be present, not dropped."""
    xwalk, _ = ids.build_xwalk(nflverse_players_df)
    row = xwalk[xwalk["gsis_id"] == "00-0005"].iloc[0]
    assert pd.isna(row["espn_player_id"])
    assert row["source"] == "players"


def test_orphans_returns_unmatched_rows_with_name_team_position(nflverse_players_df, nflverse_snaps_df):
    xwalk, _ = ids.build_xwalk(nflverse_players_df)
    reg_snaps = nflverse_snaps_df[
        (nflverse_snaps_df["game_type"] == "REG") & (nflverse_snaps_df["position"].isin(features.SKILL_POSITIONS))
    ]
    xwalk_by_pfr = xwalk.rename(columns={"pfr_id": "pfr_player_id"})

    orphans = ids.orphans(reg_snaps, xwalk_by_pfr, on="pfr_player_id", label_cols=["player", "team", "position"])

    assert list(orphans["player"]) == ["Cody White"]
    assert orphans.iloc[0]["team"] == "LV"
    assert orphans.iloc[0]["position"] == "WR"


def test_orphans_does_not_flag_resolved_players(nflverse_players_df, nflverse_snaps_df):
    xwalk, _ = ids.build_xwalk(nflverse_players_df)
    reg_snaps = nflverse_snaps_df[
        (nflverse_snaps_df["game_type"] == "REG") & (nflverse_snaps_df["position"].isin(features.SKILL_POSITIONS))
    ]
    xwalk_by_pfr = xwalk.rename(columns={"pfr_id": "pfr_player_id"})

    orphans = ids.orphans(reg_snaps, xwalk_by_pfr, on="pfr_player_id", label_cols=["player", "team", "position"])

    assert "Jayden Reed" not in set(orphans["player"])
    assert "Calvin Ridley" not in set(orphans["player"])


def test_dst_is_excluded_from_skill_positions_before_any_join():
    """nflverse has no player row for a team defence, so D/ST must never
    reach the crosswalk join in the first place -- it is filtered out by
    SKILL_POSITIONS, not reported as an orphan."""
    assert "D/ST" not in features.SKILL_POSITIONS


def test_fetch_fallback_returns_empty_frame_on_failure():
    class _BoomClient:
        def download_text(self, url):
            raise RuntimeError("network down")

    df = ids.fetch_fallback(client=_BoomClient())
    assert df.empty
    assert list(df.columns) == ["gsis_id", "espn_id"]
