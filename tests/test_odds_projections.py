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


# ---- team_totals_by_capture ------------------------------------------------

def _capture_rows(captured_at, week, min_spread, gb_spread, total):
    return [
        {"captured_at": captured_at, "week": week, "event_id": "e1", "team": "MIN",
         "market": "spreads", "book": "draftkings", "outcome_name": "Minnesota Vikings", "point": min_spread},
        {"captured_at": captured_at, "week": week, "event_id": "e1", "team": "GB",
         "market": "spreads", "book": "draftkings", "outcome_name": "Green Bay Packers", "point": gb_spread},
        {"captured_at": captured_at, "week": week, "event_id": "e1", "team": None,
         "market": "totals", "book": "draftkings", "outcome_name": "Over", "point": total},
    ]


def test_team_totals_by_capture_keeps_two_captures_distinct_not_medianed(tmp_path, monkeypatch):
    """The regression team_totals_by_capture exists to fix: build()'s
    consensus_line groups on (event_id, team, market) only, so two captures
    of the same event with different lines would median into one row.
    This accessor must instead return one row per (captured_at, team)."""
    path = tmp_path / "team_totals.parquet"
    rows = _capture_rows("2026-09-15T22:25:00+00:00", 2, -2.5, 2.5, 44.5) + \
        _capture_rows("2026-09-19T14:08:00+00:00", 2, -4.0, 4.0, 44.5)
    pd.DataFrame(rows).to_parquet(path)
    monkeypatch.setattr(projections.config, "ODDS_TEAM_TOTALS", path)

    result = projections.team_totals_by_capture(week=2)

    assert set(result["captured_at"]) == {"2026-09-15T22:25:00+00:00", "2026-09-19T14:08:00+00:00"}
    min_rows = result[result["team"] == "MIN"].sort_values("captured_at")
    assert list(min_rows["implied_team_total"]) == pytest.approx([23.5, 24.25])
    assert "event_id" not in result.columns


def test_team_totals_by_capture_filters_by_week(tmp_path, monkeypatch):
    path = tmp_path / "team_totals.parquet"
    rows = _capture_rows("2026-09-15T22:25:00+00:00", 2, -2.5, 2.5, 44.5) + \
        _capture_rows("2026-09-22T22:25:00+00:00", 3, -1.0, 1.0, 40.0)
    pd.DataFrame(rows).to_parquet(path)
    monkeypatch.setattr(projections.config, "ODDS_TEAM_TOTALS", path)

    result = projections.team_totals_by_capture(week=3)

    assert set(result["captured_at"]) == {"2026-09-22T22:25:00+00:00"}


def test_team_totals_by_capture_missing_parquet_returns_empty_with_columns(tmp_path, monkeypatch):
    monkeypatch.setattr(projections.config, "ODDS_TEAM_TOTALS", tmp_path / "team_totals.parquet")
    result = projections.team_totals_by_capture(week=2)
    assert result.empty
    assert list(result.columns) == ["captured_at", "team", "spread", "total", "implied_team_total"]


# ---- props_by_capture ------------------------------------------------------

def _prop_capture_rows(captured_at, week, point, player="Jayden Reed"):
    """One book, one line market -- enough to pin which capture a figure
    came from, which is all these tests are about."""
    return [
        {"captured_at": captured_at, "week": week, "event_id": "e1", "player_name": player,
         "team": "GB", "market": "player_receptions", "book": "draftkings",
         "outcome_name": "Over", "price": -115, "point": point},
        {"captured_at": captured_at, "week": week, "event_id": "e1", "player_name": player,
         "team": "GB", "market": "player_receptions", "book": "draftkings",
         "outcome_name": "Under", "price": -105, "point": point},
    ]


def _scoring(monkeypatch):
    monkeypatch.setattr(projections.store, "read_league_scoring", lambda: [{"stat_abbrev": "REC", "points": 1.0}])


def test_props_by_capture_keeps_two_captures_distinct_not_medianed(tmp_path, monkeypatch):
    """The reason this accessor exists. Thursday's props_primary capture and
    Sunday's pre_lock capture live in one parquet; consensus_line groups on
    (event_id, player_name, team, market) with no captured_at, so build()
    would median a 4.5 and a 6.5 into one 5.5 row and the Sunday report
    would call that "the pre_lock consensus"."""
    path = tmp_path / "player_props.parquet"
    rows = _prop_capture_rows("2026-09-17T14:08:00+00:00", 3, 4.5) + \
        _prop_capture_rows("2026-09-20T14:38:00+00:00", 3, 6.5)
    pd.DataFrame(rows).to_parquet(path)
    monkeypatch.setattr(projections.config, "ODDS_PROPS", path)
    _scoring(monkeypatch)

    result = projections.props_by_capture(week=3)

    assert set(result["captured_at"]) == {"2026-09-17T14:08:00+00:00", "2026-09-20T14:38:00+00:00"}
    by_capture = result.sort_values("captured_at")
    assert list(by_capture["point"]) == pytest.approx([4.5, 6.5])
    assert list(by_capture["fantasy_points"]) == pytest.approx([4.5, 6.5])
    assert "event_id" not in result.columns


def test_props_by_capture_filters_by_week(tmp_path, monkeypatch):
    path = tmp_path / "player_props.parquet"
    rows = _prop_capture_rows("2026-09-17T14:08:00+00:00", 3, 4.5) + \
        _prop_capture_rows("2026-09-24T14:08:00+00:00", 4, 5.5)
    pd.DataFrame(rows).to_parquet(path)
    monkeypatch.setattr(projections.config, "ODDS_PROPS", path)
    _scoring(monkeypatch)

    result = projections.props_by_capture(week=4)

    assert set(result["captured_at"]) == {"2026-09-24T14:08:00+00:00"}


def test_props_by_capture_missing_parquet_returns_empty_with_columns(tmp_path, monkeypatch):
    monkeypatch.setattr(projections.config, "ODDS_PROPS", tmp_path / "player_props.parquet")
    result = projections.props_by_capture(week=3)
    assert result.empty
    assert list(result.columns) == ["captured_at", "player_name", "team", "market", "point", "fantasy_points"]


def test_props_by_capture_output_still_resolves_through_resolve_props(tmp_path, monkeypatch):
    """Dropping event_id must not break the ids.resolve join -- player_name
    and team are the only two columns it reads, and both are kept."""
    path = tmp_path / "player_props.parquet"
    pd.DataFrame(_prop_capture_rows("2026-09-20T14:38:00+00:00", 3, 6.5)).to_parquet(path)
    monkeypatch.setattr(projections.config, "ODDS_PROPS", path)
    _scoring(monkeypatch)
    _isolate_from_disk(monkeypatch, tmp_path)

    captured = projections.props_by_capture(week=3)
    espn_players_df = pd.DataFrame([{"player_id": 4361741, "player_name": "Jayden Reed", "pro_team": "GB"}])
    out, unmatched = projections.resolve_props(captured, espn_players_df=espn_players_df)

    assert out.iloc[0]["espn_player_id"] == 4361741
    assert len(unmatched) == 0


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
