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
