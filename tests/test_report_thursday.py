"""Thursday's usage-and-market report -- the canonical-usage gate, the
props gate, the market/divergence/swap pure functions, and the
Thursday-night decisions-due wiring. Fixtures only, no network, no disk
beyond the tmp_path-scoped parquet fixtures the props-gate tests need."""

import pandas as pd
import pytest

from espn_ff.report import thursday


# ---- fixture builders (same shapes as tests/test_report_wednesday.py) ----

def _roster_row(player_id, player_name, position, lineup_slot, started,
                projected=None, week=3, team_id=5, injury_status="ACTIVE", pro_team="KC"):
    return {
        "season": 2026, "week": week, "team_id": team_id, "team_name": "Us",
        "player_id": player_id, "player_name": player_name, "position": position,
        "pro_team": pro_team, "lineup_slot_id": 0, "lineup_slot": lineup_slot,
        "started": started, "injury_status": injury_status, "acquisition_type": "DRAFT",
        "points": None, "projected": projected,
    }


def _pool_row(player_id, player_name, position, eligible_slots, week_projected, week=3):
    return {
        "player_id": player_id, "player_name": player_name, "position": position,
        "pro_team": "KC", "eligible_slots": eligible_slots, "week": week,
        "week_projected": week_projected, "percent_owned": 50.0,
        "season_points": 0.0, "season_projected": 0.0,
    }


def _feature_row(espn_player_id, player_name, position="RB", week=2, season=2026,
                 offense_pct=0.5, snap_pct_delta_1w=None, snap_pct_delta_3w=None,
                 bye=False, provisional=False, wopr=None):
    return {
        "gsis_id": f"gsis-{espn_player_id}", "season": season, "week": week,
        "player_name": player_name, "position": position, "team": "KC", "opponent": "BUF",
        "espn_player_id": espn_player_id, "played": not bye, "bye": bye, "game_completed": True,
        "offense_snaps": 40, "offense_pct": offense_pct,
        "targets": 5, "target_share": 0.2, "air_yards_share": 0.2, "wopr": wopr,
        "targets_per_snap": 0.1, "snap_pct_delta_1w": snap_pct_delta_1w,
        "snap_pct_delta_3w": snap_pct_delta_3w, "snap_pct_trend": None,
        "report_status": None, "practice_status": None, "pos_rank": None,
        "provisional": provisional,
    }


_ROSTER_SLOTS = pd.DataFrame([
    {"slot": "QB", "count": 1}, {"slot": "RB", "count": 2}, {"slot": "WR", "count": 2},
    {"slot": "RB/WR", "count": 1}, {"slot": "TE", "count": 1}, {"slot": "D/ST", "count": 1},
    {"slot": "K", "count": 1}, {"slot": "Bench", "count": 6}, {"slot": "IR", "count": 1},
])
_ALLOWED = {"QB", "RB", "WR", "RB/WR", "TE", "D/ST", "K", "Bench", "IR"}

_EMPTY_MARKET = {"by_player": {}, "unmatched_count": 0}
_EMPTY_PROPS_GATE = {"insufficient": True, "reason": "no player_props.parquet on disk", "props_df": pd.DataFrame()}
_EMPTY_GATE = {"insufficient": True, "reason": "no player_week_features rows for week 2", "provisional": None}


def _render(**overrides):
    """render() call with every argument defaulted to its empty/insufficient
    shape, so a test only names the fields it cares about."""
    kwargs = dict(
        season=2026, week=3, team_id=5, usage_week=2,
        thursday_games=pd.DataFrame(), tnf_rows=[], watch_rows=[],
        gate=dict(_EMPTY_GATE), features_df=pd.DataFrame(),
        props_gate=dict(_EMPTY_PROPS_GATE), market=dict(_EMPTY_MARKET),
        divergence_rows=[], could_not_compare=0, swap_rows=[],
        practice_df=pd.DataFrame(), footer_notes=thursday.FOOTER_NOTES,
        rendered_at=1_760_000_000,
    )
    kwargs.update(overrides)
    return thursday.render(**kwargs)


# ---- _round_or_none: the gap-vs-zero trap ---------------------------------

def test_round_or_none_passes_nan_and_none_through_untouched():
    assert thursday._round_or_none(float("nan")) is None
    assert thursday._round_or_none(None) is None
    assert thursday._round_or_none(0.123456, digits=3) == 0.123


# ---- canonical_read --------------------------------------------------------

def test_canonical_read_empty_names_nflverse_fetch_time(monkeypatch):
    monkeypatch.setattr(thursday.loaders, "nflverse_features_freshness", lambda season=None: 1_760_000_000)
    gate = thursday.canonical_read(pd.DataFrame(), usage_week=2)
    assert gate["insufficient"] is True
    assert "week 2" in gate["reason"]
    assert "ET" in gate["reason"]


def test_canonical_read_empty_and_never_fetched_says_so(monkeypatch):
    monkeypatch.setattr(thursday.loaders, "nflverse_features_freshness", lambda season=None: None)
    gate = thursday.canonical_read(pd.DataFrame(), usage_week=2)
    assert gate["insufficient"] is True
    assert "never been fetched" in gate["reason"]


def test_canonical_read_provisional_true_still_renders_not_insufficient():
    features_df = pd.DataFrame([_feature_row(1, "P1", provisional=True)])
    gate = thursday.canonical_read(features_df, usage_week=2)
    assert gate["insufficient"] is False
    assert gate["provisional"] is True


def test_canonical_read_provisional_false():
    features_df = pd.DataFrame([_feature_row(1, "P1", provisional=False)])
    gate = thursday.canonical_read(features_df, usage_week=2)
    assert gate["insufficient"] is False
    assert gate["provisional"] is False


# ---- props_read: each reason separately -----------------------------------

def test_props_read_parquet_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(thursday.config, "ODDS_PROPS", tmp_path / "player_props.parquet")
    gate = thursday.props_read(3)
    assert gate["insufficient"] is True
    assert "no player_props.parquet on disk" in gate["reason"]


def test_props_read_last_run_absent(tmp_path, monkeypatch):
    path = tmp_path / "player_props.parquet"
    pd.DataFrame([{"event_id": "e1"}]).to_parquet(path)
    monkeypatch.setattr(thursday.config, "ODDS_PROPS", path)
    monkeypatch.setattr(thursday.odds_store, "read_last_run", lambda job=None: None)
    gate = thursday.props_read(3)
    assert gate["insufficient"] is True
    assert "no props_primary entry" in gate["reason"]


def test_props_read_last_run_stale(tmp_path, monkeypatch):
    path = tmp_path / "player_props.parquet"
    pd.DataFrame([{"event_id": "e1"}]).to_parquet(path)
    monkeypatch.setattr(thursday.config, "ODDS_PROPS", path)
    monkeypatch.setattr(
        thursday.odds_store, "read_last_run",
        lambda job=None: {"job": "props_primary", "stale": True, "reason": "budget aborted"},
    )
    gate = thursday.props_read(3)
    assert gate["insufficient"] is True
    assert "budget aborted" in gate["reason"]


def test_props_read_frame_empty_for_week(tmp_path, monkeypatch):
    path = tmp_path / "player_props.parquet"
    pd.DataFrame([{"event_id": "e1"}]).to_parquet(path)
    monkeypatch.setattr(thursday.config, "ODDS_PROPS", path)
    monkeypatch.setattr(thursday.odds_store, "read_last_run", lambda job=None: {"job": "props_primary", "stale": False})
    monkeypatch.setattr(thursday.odds_projections, "build", lambda week=None: (pd.DataFrame(), pd.DataFrame()))
    gate = thursday.props_read(3)
    assert gate["insufficient"] is True
    assert "has not consolidated" in gate["reason"]


def test_props_read_success(tmp_path, monkeypatch):
    path = tmp_path / "player_props.parquet"
    pd.DataFrame([{"event_id": "e1"}]).to_parquet(path)
    monkeypatch.setattr(thursday.config, "ODDS_PROPS", path)
    monkeypatch.setattr(thursday.odds_store, "read_last_run", lambda job=None: {"job": "props_primary", "stale": False})
    props_df = pd.DataFrame([{"event_id": "e1", "player_name": "X", "market": "player_receptions",
                              "point": 4.0, "fantasy_points": 2.0}])
    monkeypatch.setattr(thursday.odds_projections, "build", lambda week=None: (props_df, pd.DataFrame()))
    gate = thursday.props_read(3)
    assert gate["insufficient"] is False
    assert len(gate["props_df"]) == 1


# ---- market_points ----------------------------------------------------------

def test_market_points_sums_fantasy_points_across_markets_and_counts_unmatched():
    props_df = pd.DataFrame([
        {"event_id": "e1", "player_name": "Jayden Reed", "team": None,
         "market": "player_receptions", "point": 4.5, "fantasy_points": 2.25},
        {"event_id": "e1", "player_name": "Jayden Reed", "team": None,
         "market": "player_reception_yds", "point": 55.5, "fantasy_points": 5.55},
        {"event_id": "e1", "player_name": "Some Rookie", "team": None,
         "market": "player_receptions", "point": 2.0, "fantasy_points": 1.0},
    ])
    pool_df = pd.DataFrame([{"player_id": 1, "player_name": "Jayden Reed", "pro_team": "GB"}])

    # Isolate from whatever real player_xwalk.csv/player_id_map.csv happen to be
    # on disk in this checkout -- the espn_pool path (pool_df above) is what
    # this test means to exercise.
    result = thursday.market_points(
        props_df, pool_df, xwalk_df=pd.DataFrame(), sleeper_map_df=pd.DataFrame()
    )

    assert result["by_player"][1]["points"] == pytest.approx(7.8)
    assert result["by_player"][1]["markets"] == 2
    assert result["unmatched_count"] == 1
    assert 2 not in result["by_player"]  # unmatched, never given points


def test_market_points_empty_input_returns_empty_shape():
    result = thursday.market_points(pd.DataFrame(), pd.DataFrame())
    assert result == {"by_player": {}, "unmatched_count": 0}


# ---- divergence ---------------------------------------------------------------

def test_divergence_fires_at_exactly_25_percent_not_below():
    market_by_player = {
        1: {"points": 12.5, "markets": 1, "player_name": "A"},   # |12.5-10|/10 = 25% -> fires
        2: {"points": 11.0, "markets": 1, "player_name": "B"},   # 10% -> does not fire
    }
    pool_df = pd.DataFrame([
        {"player_id": 1, "player_name": "A", "week_projected": 10.0},
        {"player_id": 2, "player_name": "B", "week_projected": 10.0},
    ])
    rows, could_not_compare = thursday.divergence(market_by_player, pool_df)
    assert [r["player_name"] for r in rows] == ["A"]
    assert could_not_compare == 0


def test_divergence_zero_or_nan_espn_projection_could_not_compare():
    market_by_player = {
        1: {"points": 10.0, "markets": 1, "player_name": "A"},
        2: {"points": 5.0, "markets": 1, "player_name": "B"},
    }
    pool_df = pd.DataFrame([
        {"player_id": 1, "player_name": "A", "week_projected": 0.0},
        {"player_id": 2, "player_name": "B", "week_projected": float("nan")},
    ])
    rows, could_not_compare = thursday.divergence(market_by_player, pool_df)
    assert rows == []
    assert could_not_compare == 2


# ---- swap_candidates ------------------------------------------------------

def test_swap_candidates_pairs_falling_starter_with_rising_same_slot_bench():
    features_by_id = {
        1: {"snap_pct_delta_1w": -0.15, "wopr": 0.3},
        2: {"snap_pct_delta_1w": 0.20, "wopr": 0.5},
        3: {"snap_pct_delta_1w": 0.05, "wopr": 0.1},
    }
    starters = pd.DataFrame([_roster_row(1, "Fading Starter", "WR", "WR", True, projected=10.0)])
    bench = pd.DataFrame([
        _roster_row(2, "Rising Bench", "WR", "Bench", False, projected=12.0),
        _roster_row(3, "Flat Bench", "RB", "Bench", False, projected=8.0),
    ])
    pool_df = pd.DataFrame([
        _pool_row(1, "Fading Starter", "WR", "WR, RB/WR, Bench", 10.0),
        _pool_row(2, "Rising Bench", "WR", "WR, RB/WR, Bench", 12.0),
        _pool_row(3, "Flat Bench", "RB", "RB, RB/WR, Bench", 8.0),
    ])
    rows = thursday.swap_candidates(starters, bench, features_by_id, pool_df, _ALLOWED)
    assert len(rows) == 1
    assert rows[0]["starter_name"] == "Fading Starter"
    assert rows[0]["bench_name"] == "Rising Bench"
    assert rows[0]["gap"] == pytest.approx(2.0)


def test_swap_candidates_none_when_no_starter_delta_fell():
    features_by_id = {1: {"snap_pct_delta_1w": 0.10, "wopr": 0.3}}
    starters = pd.DataFrame([_roster_row(1, "Steady Starter", "WR", "WR", True, projected=10.0)])
    bench = pd.DataFrame([_roster_row(2, "Bench Guy", "WR", "Bench", False, projected=8.0)])
    pool_df = pd.DataFrame([
        _pool_row(1, "Steady Starter", "WR", "WR, RB/WR, Bench", 10.0),
        _pool_row(2, "Bench Guy", "WR", "WR, RB/WR, Bench", 8.0),
    ])
    rows = thursday.swap_candidates(starters, bench, features_by_id, pool_df, _ALLOWED)
    assert rows == []


# ---- render: zero games, provisional banner, the gap-not-zero requirements --

def test_zero_thursday_games_renders_explicit_line():
    text = _render(thursday_games=pd.DataFrame())
    assert "No Thursday-night game in week 3." in text


def test_provisional_banner_renders_with_table():
    features_df = pd.DataFrame([_feature_row(1, "P1", offense_pct=0.55)])
    gate = {"insufficient": False, "reason": None, "provisional": True}
    text = _render(gate=gate, features_df=features_df)
    assert "still **provisional**" in text
    assert "P1" in text


def test_canonical_usage_table_renders_gap_for_nan_snap_delta_not_zero():
    row = _feature_row(1, "P1", offense_pct=0.5, snap_pct_delta_1w=0.02, snap_pct_delta_3w=float("nan"))
    features_df = pd.DataFrame([row])
    gate = {"insufficient": False, "reason": None, "provisional": False}
    text = _render(gate=gate, features_df=features_df)
    line = next(l for l in text.splitlines() if l.startswith("| P1"))
    cells = [c.strip() for c in line.strip("|").split("|")]
    # headers: player, pos, offense_pct, snap_pct_delta_1w, snap_pct_delta_3w, ...
    assert cells[4] == "--"


def test_bye_week_renders_null_offense_pct_never_zero_percent():
    row = _feature_row(1, "Bye Guy", offense_pct=0.0, bye=True)
    features_df = pd.DataFrame([row])
    gate = {"insufficient": False, "reason": None, "provisional": False}
    text = _render(gate=gate, features_df=features_df)
    line = next(l for l in text.splitlines() if l.startswith("| Bye Guy"))
    cells = [c.strip() for c in line.strip("|").split("|")]
    assert cells[2] == "--"
    assert "On bye: Bye Guy" in text


def test_render_with_every_frame_empty_produces_complete_document():
    text = _render()
    assert "# Usage and market -- 2026 week 3" in text
    assert "## Decisions due" in text
    assert "## Canonical usage -- week 2" in text
    assert "## Market -- prop-derived points" in text
    assert "## Divergence -- prop points vs ESPN projection" in text
    assert "## Swap candidates" in text
    assert "## Drop candidates" in text
    assert "## Practice report -- Wed / Thu" in text
    assert "## What this report cannot see" in text
    assert thursday.INSUFFICIENT_DATA in text


# ---- build(): at-risk TNF starter paired with best legal replacement,
# never IR ---------------------------------------------------------------

def test_build_pairs_at_risk_tnf_starter_with_best_legal_bench_never_ir(monkeypatch):
    exports = {
        "weekly-rosters": pd.DataFrame([
            _roster_row(1, "Hurt Starter", "RB", "RB", True, projected=14.0, pro_team="KC"),
            _roster_row(2, "Good Backup", "RB", "Bench", False, projected=9.0, pro_team="KC"),
            _roster_row(3, "Stashed", "RB", "IR", False, projected=20.0, pro_team="KC"),
            _roster_row(4, "Weak Backup", "RB", "Bench", False, projected=4.0, pro_team="BUF"),
        ]),
        "player-pool": pd.DataFrame([
            _pool_row(1, "Hurt Starter", "RB", "RB, RB/WR, Bench", 14.0),
            _pool_row(2, "Good Backup", "RB", "RB, RB/WR, Bench", 9.0),
            _pool_row(3, "Stashed", "RB", "RB, RB/WR, Bench", 20.0),
            _pool_row(4, "Weak Backup", "RB", "RB, RB/WR, Bench", 4.0),
        ]),
        "roster-slots": _ROSTER_SLOTS,
    }
    monkeypatch.setattr(thursday, "latest_export", lambda name: exports.get(name, pd.DataFrame()))
    monkeypatch.setattr(
        thursday.schedule, "remaining_games",
        lambda season, week, weekday="Monday": (
            pd.DataFrame([{"home_team": "KC", "away_team": "BUF", "gametime": "20:15"}])
            if weekday == "Thursday" else pd.DataFrame()
        ),
    )
    monkeypatch.setattr(
        thursday.availability, "read",
        lambda players_df, season, week: pd.DataFrame([
            {"player_id": 1, "tier": "HIGH_RISK"}, {"player_id": 2, "tier": "CLEAR"},
            {"player_id": 3, "tier": "CLEAR"}, {"player_id": 4, "tier": "CLEAR"},
        ]),
    )
    monkeypatch.setattr(
        thursday.wednesday, "practice_signals",
        lambda players_df, today=None: pd.DataFrame(
            [{"player_id": pid, "practice_trajectory": "DNP / LP / --", "matched": True} for pid in [1, 2, 3, 4]]
        ),
    )
    monkeypatch.setattr(thursday.loaders, "features", lambda season, week: pd.DataFrame())
    monkeypatch.setattr(thursday.loaders, "nflverse_features_freshness", lambda season=None: None)
    monkeypatch.setattr(
        thursday, "props_read",
        lambda week: {"insufficient": True, "reason": "no player_props.parquet on disk", "props_df": pd.DataFrame()},
    )
    monkeypatch.setattr(thursday, "freshness", lambda season=None: {
        "sleeper": (None, True), "nflverse": (None, True), "espn": (None, True), "odds": (None, True),
    })
    monkeypatch.setattr(thursday, "espn_export_warning", lambda: None)

    text = thursday.build(2026, week=3, team_id=5)

    decisions_section = text.split("## Decisions due")[1].split("## Canonical usage")[0]
    assert "best legal swap: Good Backup" in decisions_section
    assert "Stashed" not in decisions_section  # IR is never a candidate, and never shown as roster context either
