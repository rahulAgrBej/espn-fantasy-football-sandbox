"""espn_ff.odds.projections -- pure, offline, fixtures only."""

import pandas as pd
import pytest

from espn_ff.odds import projections


def test_devig_two_way_normalizes_to_one():
    p_yes, p_no = projections.devig_two_way(145, -180)
    assert p_yes + p_no == pytest.approx(1.0)
    assert 0 < p_yes < 1


def test_devig_two_way_favorite_has_higher_probability():
    p_yes, p_no = projections.devig_two_way(-200, 150)
    assert p_yes > p_no


def _line_rows():
    return pd.DataFrame(
        [
            {"event_id": "e1", "player_name": "Jayden Reed", "team": None, "market": "player_receptions",
             "book": "draftkings", "outcome_name": "Over", "price": -115, "point": 4.5},
            {"event_id": "e1", "player_name": "Jayden Reed", "team": None, "market": "player_receptions",
             "book": "draftkings", "outcome_name": "Under", "price": -105, "point": 4.5},
            {"event_id": "e1", "player_name": "Jayden Reed", "team": None, "market": "player_receptions",
             "book": "fanduel", "outcome_name": "Over", "price": -120, "point": 5.0},
            {"event_id": "e1", "player_name": "Jayden Reed", "team": None, "market": "player_receptions",
             "book": "fanduel", "outcome_name": "Under", "price": 100, "point": 5.0},
        ]
    )


def _prob_rows():
    return pd.DataFrame(
        [
            {"event_id": "e1", "player_name": "Jayden Reed", "team": None, "market": "player_anytime_td",
             "book": "draftkings", "outcome_name": "Yes", "price": 145, "point": None},
            {"event_id": "e1", "player_name": "Jayden Reed", "team": None, "market": "player_anytime_td",
             "book": "draftkings", "outcome_name": "No", "price": -180, "point": None},
            {"event_id": "e1", "player_name": "Jayden Reed", "team": None, "market": "player_anytime_td",
             "book": "fanduel", "outcome_name": "Yes", "price": 150, "point": None},
            {"event_id": "e1", "player_name": "Jayden Reed", "team": None, "market": "player_anytime_td",
             "book": "fanduel", "outcome_name": "No", "price": -190, "point": None},
        ]
    )


def test_consensus_line_medians_the_point_across_books_for_line_markets():
    result = projections.consensus_line(_line_rows())
    assert len(result) == 1
    assert result.iloc[0]["point"] == pytest.approx(4.75)  # median of 4.5, 5.0


def test_consensus_line_devigs_then_medians_probability_markets():
    result = projections.consensus_line(_prob_rows())
    assert len(result) == 1
    p_dk, _ = projections.devig_two_way(145, -180)
    p_fd, _ = projections.devig_two_way(150, -190)
    assert result.iloc[0]["point"] == pytest.approx(sorted([p_dk, p_fd])[0] + (sorted([p_dk, p_fd])[1] - sorted([p_dk, p_fd])[0]) / 2)


def _real_shape_spread_rows():
    """The shape _flatten_featured actually produces: one row per
    (event, team, book), with `outcome_name` set to that team's own full
    name -- never "Over"/"Yes". Regression for consensus_line treating
    spreads like a two-sided Over/Yes market and returning NaN for every
    team, which real captured data hit 100% of the time."""
    return pd.DataFrame(
        [
            {"event_id": "e1", "team": "MIN", "market": "spreads", "book": "draftkings",
             "outcome_name": "Minnesota Vikings", "point": -2.5},
            {"event_id": "e1", "team": "MIN", "market": "spreads", "book": "fanduel",
             "outcome_name": "Minnesota Vikings", "point": -3.0},
            {"event_id": "e1", "team": "GB", "market": "spreads", "book": "draftkings",
             "outcome_name": "Green Bay Packers", "point": 2.5},
            {"event_id": "e1", "team": "GB", "market": "spreads", "book": "fanduel",
             "outcome_name": "Green Bay Packers", "point": 3.0},
        ]
    )


def test_consensus_line_medians_spreads_by_point_not_by_outcome_label():
    result = projections.consensus_line(_real_shape_spread_rows())
    assert len(result) == 2
    by_team = dict(zip(result["team"], result["point"]))
    assert by_team["MIN"] == pytest.approx(-2.75)
    assert by_team["GB"] == pytest.approx(2.75)


def _spread_total_rows():
    return pd.DataFrame(
        [
            {"event_id": "e1", "player_name": None, "team": "Minnesota Vikings", "market": "spreads", "point": -2.5},
            {"event_id": "e1", "player_name": None, "team": "Green Bay Packers", "market": "spreads", "point": 2.5},
            {"event_id": "e1", "player_name": None, "team": None, "market": "totals", "point": 44.5},
        ]
    )


def test_implied_team_totals_favorite_and_underdog_sum_to_the_total():
    result = projections.implied_team_totals(_spread_total_rows())
    fav = result[result["team"] == "Minnesota Vikings"].iloc[0]
    dog = result[result["team"] == "Green Bay Packers"].iloc[0]
    assert fav["implied_team_total"] == pytest.approx(23.5)
    assert dog["implied_team_total"] == pytest.approx(21.0)
    assert fav["implied_team_total"] + dog["implied_team_total"] == pytest.approx(44.5)


def test_prop_to_points_uses_league_scoring_for_line_markets():
    consensus = pd.DataFrame([{"market": "player_receptions", "point": 5.0}])
    scoring = [{"stat_abbrev": "REC", "points": 0.5}]
    result = projections.prop_to_points(consensus, scoring)
    assert result.iloc[0]["fantasy_points"] == pytest.approx(2.5)


def test_prop_to_points_multiplies_devigged_probability_by_leagues_own_td_value_not_six():
    consensus = pd.DataFrame([{"market": "player_anytime_td", "point": 0.4}])
    scoring = [{"stat_abbrev": "TD", "points": 8}]  # a non-standard league setting
    result = projections.prop_to_points(consensus, scoring)
    assert result.iloc[0]["fantasy_points"] == pytest.approx(3.2)


def test_prop_to_points_passes_through_points_markets_unconverted():
    consensus = pd.DataFrame([{"market": "player_kicking_points", "point": 7.5}])
    result = projections.prop_to_points(consensus, scoring=[{"stat_abbrev": "PTS_KICK", "points": 999}])
    assert result.iloc[0]["fantasy_points"] == 7.5


def test_build_returns_empty_frames_with_nothing_on_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(projections.config, "ODDS_DIR", tmp_path)
    monkeypatch.setattr(projections.config, "ODDS_PROPS", tmp_path / "player_props.parquet")
    monkeypatch.setattr(projections.config, "ODDS_TEAM_TOTALS", tmp_path / "team_totals.parquet")
    monkeypatch.setattr(projections.config, "ODDS_SCORING", tmp_path / "league_scoring.json")

    props_points, team_totals_points = projections.build(week=3)
    assert props_points.empty
    assert team_totals_points.empty


# ---- resolve_props -------------------------------------------------------

def _props_points_df():
    return pd.DataFrame(
        [
            {"event_id": "e1", "player_name": "Jayden Reed", "team": None, "market": "player_receptions",
             "point": 4.75, "fantasy_points": 2.375},
            {"event_id": "e1", "player_name": "Some Rookie Nobody Has Heard Of", "team": None,
             "market": "player_receptions", "point": 2.0, "fantasy_points": 1.0},
        ]
    )


def _isolate_from_disk(monkeypatch, tmp_path):
    """This repo's real data/nflverse/player_xwalk.csv and
    data/sleeper/player_id_map.csv may carry a real "Jayden Reed" row of
    their own, which would outrank the espn_pool path these tests mean to
    exercise (xwalk is tried first). Point both at nonexistent tmp paths so
    resolve_props's disk defaults degrade to empty, same contract
    test_resolve_props_missing_crosswalk_files_degrade_to_empty_index below
    pins directly."""
    monkeypatch.setattr(projections.config, "NFLVERSE_XWALK", tmp_path / "player_xwalk.csv")
    monkeypatch.setattr(projections.config, "SLEEPER_ID_MAP", tmp_path / "player_id_map.csv")


def test_resolve_props_attaches_espn_id_and_match_source_columns(tmp_path, monkeypatch):
    _isolate_from_disk(monkeypatch, tmp_path)
    espn_players_df = pd.DataFrame([{"player_id": 4361741, "player_name": "Jayden Reed", "pro_team": "GB"}])
    out, unmatched = projections.resolve_props(_props_points_df(), espn_players_df=espn_players_df)
    assert {"espn_player_id", "match_source"} <= set(out.columns)
    row = out[out["player_name"] == "Jayden Reed"].iloc[0]
    assert row["espn_player_id"] == 4361741
    assert row["match_source"] == "espn_pool"


def test_resolve_props_keeps_unmatched_rows_rather_than_dropping_them(tmp_path, monkeypatch):
    _isolate_from_disk(monkeypatch, tmp_path)
    espn_players_df = pd.DataFrame([{"player_id": 4361741, "player_name": "Jayden Reed", "pro_team": "GB"}])
    out, unmatched = projections.resolve_props(_props_points_df(), espn_players_df=espn_players_df)
    assert len(out) == len(_props_points_df())  # nothing dropped

    ghost = out[out["player_name"] == "Some Rookie Nobody Has Heard Of"].iloc[0]
    assert ghost["match_source"] == "unmatched"
    assert pd.isna(ghost["espn_player_id"])
    assert len(unmatched) == 1


def test_resolve_props_missing_crosswalk_files_degrade_to_empty_index(tmp_path, monkeypatch):
    """No player_xwalk.csv / player_id_map.csv on disk must never raise --
    the espn_pool path (the caller-supplied espn_players_df) still resolves
    what it can, same "degrade gracefully" contract as
    report/availability.py's _xwalk()/_id_map()."""
    monkeypatch.setattr(projections.config, "NFLVERSE_XWALK", tmp_path / "player_xwalk.csv")
    monkeypatch.setattr(projections.config, "SLEEPER_ID_MAP", tmp_path / "player_id_map.csv")

    espn_players_df = pd.DataFrame([{"player_id": 4361741, "player_name": "Jayden Reed", "pro_team": "GB"}])
    out, unmatched = projections.resolve_props(_props_points_df(), espn_players_df=espn_players_df)

    row = out[out["player_name"] == "Jayden Reed"].iloc[0]
    assert row["espn_player_id"] == 4361741
    assert row["match_source"] == "espn_pool"
