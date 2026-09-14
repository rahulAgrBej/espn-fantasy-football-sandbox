"""Derived nflverse role features -- fixtures only, no network.

Fixture universe: 5 teams (GB, MIN, LV, DEN, SEA) across 3 weeks, engineered
so each team byes exactly once (GB never byes; MIN byes week 3; DEN byes
week 2; SEA byes week 1) and week 3's two games are incomplete
(home_score/away_score null), so week 3 rows must read provisional.
"""

import pandas as pd
import pytest

from espn_ff.nflverse import features, store

XWALK_COLUMNS = ["gsis_id", "pfr_id", "espn_player_id", "display_name", "position", "latest_team", "status", "source"]


@pytest.fixture
def nflverse_xwalk_df(nflverse_players_df):
    from espn_ff.nflverse import ids
    xwalk, _ = ids.build_xwalk(nflverse_players_df)
    return xwalk


@pytest.fixture
def built_features(monkeypatch, nflverse_snaps_df, nflverse_stats_df, nflverse_games_df, nflverse_xwalk_df):
    empty_injuries = pd.DataFrame(columns=["season", "week", "team", "gsis_id", "report_status", "practice_status"])
    empty_depth = pd.DataFrame(columns=["dt", "team", "gsis_id", "pos_abb", "pos_rank"])

    def fake_load(name, season=None):
        return {
            "schedules": nflverse_games_df,
            "snap_counts": nflverse_snaps_df,
            "stats_player": nflverse_stats_df,
            "injuries": empty_injuries,
            "depth_charts": empty_depth,
        }[name]

    monkeypatch.setattr(store, "load", fake_load)
    monkeypatch.setattr(features, "_read_xwalk", lambda: nflverse_xwalk_df)
    return features.build([2026])


def _row(df, gsis_id, week):
    match = df[(df["gsis_id"] == gsis_id) & (df["week"] == week)]
    assert len(match) == 1, f"expected exactly one row for {gsis_id} week {week}, got {len(match)}"
    return match.iloc[0]


def test_snap_row_with_no_stats_row_yields_zero_not_null(built_features):
    """Calvin Ridley's week-1 row: snaps recorded, no stats_player row at
    all. Missing production is a real zero, not missing data."""
    row = _row(built_features, "00-0002", 1)
    assert row["offense_snaps"] == 32
    assert row["offense_pct"] == pytest.approx(0.64)
    assert row["targets"] == 0
    assert row["target_share"] == 0
    assert not pd.isna(row["targets"])


def test_bye_week_is_not_a_zero_percent_snap_row(built_features):
    """DEN byes week 2 -- Calvin Ridley's week-2 row must read bye=True with
    a null offense_pct, never offense_pct = 0."""
    row = _row(built_features, "00-0002", 2)
    assert row["bye"] is True or row["bye"] == True  # noqa: E712 -- numpy bool
    assert row["played"] is False or row["played"] == False  # noqa: E712
    assert pd.isna(row["offense_pct"])
    assert pd.isna(row["offense_snaps"])


def test_no_zero_percent_rows_are_produced_for_any_bye(built_features):
    bye_rows = built_features[built_features["bye"]]
    assert not bye_rows.empty
    assert bye_rows["offense_pct"].isna().all()


def test_snap_pct_delta_1w_skips_the_bye(built_features):
    """Ridley: 0.64 week 1, bye week 2, 0.70 week 3. The delta at week 3
    must compare against week 1 (+0.06), not treat the bye as a 0."""
    row = _row(built_features, "00-0002", 3)
    assert row["snap_pct_delta_1w"] == pytest.approx(0.06)


def test_snap_pct_trend_is_null_with_fewer_than_three_appearances(built_features):
    # Ridley and Jefferson each only ever play 2 games this fixture season.
    ridley_week3 = _row(built_features, "00-0002", 3)
    assert pd.isna(ridley_week3["snap_pct_trend"])

    jefferson_week2 = _row(built_features, "00-0003", 2)
    assert pd.isna(jefferson_week2["snap_pct_trend"])


def test_snap_pct_trend_is_populated_on_the_third_appearance(built_features):
    # Jayden Reed plays all 3 weeks -- his week-3 row is his 3rd appearance.
    row = _row(built_features, "00-0001", 3)
    assert not pd.isna(row["snap_pct_trend"])


def test_targets_per_snap_is_null_not_a_division_error_at_zero_snaps(built_features):
    row = _row(built_features, "00-0004", 2)  # Bench Guy: 0 offense_snaps
    assert row["offense_snaps"] == 0
    assert pd.isna(row["targets_per_snap"])


def test_provisional_flips_false_only_when_every_game_that_week_is_complete(built_features):
    week1 = built_features[built_features["week"] == 1]
    week2 = built_features[built_features["week"] == 2]
    week3 = built_features[built_features["week"] == 3]  # both week-3 games are incomplete

    assert (~week1["provisional"]).all()
    assert (~week2["provisional"]).all()
    assert week3["provisional"].all()


def test_trailing_bye_after_last_appearance_is_absent_not_a_bye_row(built_features):
    """MIN byes week 3, but Justin Jefferson's last recorded appearance is
    week 2 -- there is no later appearance to prove he is still on the team,
    so week 3 must be absent entirely, not surfaced as bye=True. This is the
    opposite case from Calvin Ridley's week-2 bye, which sits *between* two
    known appearances and does get a bye row (see test above)."""
    match = built_features[(built_features["gsis_id"] == "00-0003") & (built_features["week"] == 3)]
    assert match.empty


def test_orphan_snap_row_never_enters_the_feature_table(built_features):
    # Cody White has no players.csv row at all -- never resolves to a gsis_id.
    assert "Cody White" not in set(built_features["player_name"])
