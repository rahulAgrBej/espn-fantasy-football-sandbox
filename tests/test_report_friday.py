"""Friday's lineup-lock report -- the line-movement gate, the lineup solve
built on projections (not settled points), the slot-name diff that must
never report a spurious swap, held-open slots, and the render/build wiring.
Fixtures only, no network, no disk beyond the tmp_path-scoped parquet the
gate tests need."""

import pandas as pd
import pytest

from espn_ff.report import friday, tuesday


# ---- fixture builders (same shapes as tests/test_report_thursday.py) ------

def _roster_row(player_id, player_name, position, lineup_slot, started,
                week=3, team_id=5, injury_status="ACTIVE", pro_team="KC"):
    return {
        "season": 2026, "week": week, "team_id": team_id, "team_name": "Us",
        "player_id": player_id, "player_name": player_name, "position": position,
        "pro_team": pro_team, "lineup_slot_id": 0, "lineup_slot": lineup_slot,
        "started": started, "injury_status": injury_status, "acquisition_type": "DRAFT",
        "points": None, "projected": None,
    }


def _pool_row(player_id, player_name, position, eligible_slots, week_projected, week=3):
    return {
        "player_id": player_id, "player_name": player_name, "position": position,
        "pro_team": "KC", "eligible_slots": eligible_slots, "week": week,
        "week_projected": week_projected, "percent_owned": 50.0,
        "season_points": 0.0, "season_projected": 0.0,
    }


_ROSTER_SLOTS = pd.DataFrame([
    {"slot": "QB", "count": 1}, {"slot": "RB", "count": 2}, {"slot": "WR", "count": 2},
    {"slot": "RB/WR", "count": 1}, {"slot": "TE", "count": 1}, {"slot": "D/ST", "count": 1},
    {"slot": "K", "count": 1}, {"slot": "Bench", "count": 6}, {"slot": "IR", "count": 1},
])
_ALLOWED = {"QB", "RB", "WR", "RB/WR", "TE", "D/ST", "K", "Bench", "IR"}

_EMPTY_MOVEMENT = {"insufficient": True, "reason": "no team_totals.parquet on disk", "rows": [],
                    "open_at": None, "current_at": None}
_EMPTY_RECOMMENDED = {
    "insufficient": True, "reason": "no weekly-rosters or roster-slots export for this week",
    "lineup": [], "unfilled_slots": [], "unprojected_names": set(), "fallback_names": set(),
    "candidates": [], "eligibility": [], "values": [],
}


def _render(**overrides):
    """render() call with every argument defaulted to its empty/insufficient
    shape, so a test only names the fields it cares about."""
    kwargs = dict(
        season=2026, week=3, team_id=5,
        movement=dict(_EMPTY_MOVEMENT), recommended=dict(_EMPTY_RECOMMENDED),
        diff_rows=[], held_rows=[], bench_rows=[], practice_df=pd.DataFrame(),
        streaming_drops=[], streaming_drop_reason="no legal drop candidate exists this week",
        footer_notes=friday.FOOTER_NOTES, rendered_at=1_760_000_000,
    )
    kwargs.update(overrides)
    return friday.render(**kwargs)


# ---- line_movement_read: one test per rung of the ladder -------------------

def test_movement_read_parquet_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(friday.config, "ODDS_TEAM_TOTALS", tmp_path / "team_totals.parquet")
    gate = friday.line_movement_read(3)
    assert gate["insufficient"] is True
    assert "no team_totals.parquet on disk" in gate["reason"]


def test_movement_read_no_line_movement_entry(tmp_path, monkeypatch):
    path = tmp_path / "team_totals.parquet"
    pd.DataFrame([{"captured_at": "2026-09-15T22:25:00+00:00", "event_id": "e1", "team": "MIN",
                   "market": "spreads", "point": -2.5}]).to_parquet(path)
    monkeypatch.setattr(friday.config, "ODDS_TEAM_TOTALS", path)
    monkeypatch.setattr(friday.odds_store, "read_last_run", lambda job=None: None)
    gate = friday.line_movement_read(3)
    assert gate["insufficient"] is True
    assert "no line_movement entry" in gate["reason"]


def test_movement_read_last_run_stale(tmp_path, monkeypatch):
    path = tmp_path / "team_totals.parquet"
    pd.DataFrame([{"captured_at": "2026-09-15T22:25:00+00:00", "event_id": "e1", "team": "MIN",
                   "market": "spreads", "point": -2.5}]).to_parquet(path)
    monkeypatch.setattr(friday.config, "ODDS_TEAM_TOTALS", path)
    monkeypatch.setattr(
        friday.odds_store, "read_last_run",
        lambda job=None: {"job": "line_movement", "stale": True, "reason": "budget aborted"},
    )
    gate = friday.line_movement_read(3)
    assert gate["insufficient"] is True
    assert "budget aborted" in gate["reason"]


def test_movement_read_no_rows_for_week(tmp_path, monkeypatch):
    path = tmp_path / "team_totals.parquet"
    pd.DataFrame([{"captured_at": "2026-09-15T22:25:00+00:00", "week": 2, "event_id": "e1", "team": "MIN",
                   "market": "spreads", "point": -2.5}]).to_parquet(path)
    monkeypatch.setattr(friday.config, "ODDS_TEAM_TOTALS", path)
    monkeypatch.setattr(friday.odds_store, "read_last_run", lambda job=None: {"job": "line_movement", "stale": False})
    gate = friday.line_movement_read(3)
    assert gate["insufficient"] is True
    assert "no team_totals rows for week 3" in gate["reason"]


def _two_team_rows(captured_at, week, min_spread, gb_spread, total):
    return [
        {"captured_at": captured_at, "week": week, "event_id": "e1", "team": "MIN",
         "market": "spreads", "book": "draftkings", "outcome_name": "Minnesota Vikings", "point": min_spread},
        {"captured_at": captured_at, "week": week, "event_id": "e1", "team": "GB",
         "market": "spreads", "book": "draftkings", "outcome_name": "Green Bay Packers", "point": gb_spread},
        {"captured_at": captured_at, "week": week, "event_id": "e1", "team": None,
         "market": "totals", "book": "draftkings", "outcome_name": "Over", "point": total},
    ]


def test_movement_read_exactly_one_capture_names_the_capture_time_not_a_zero_delta(tmp_path, monkeypatch):
    path = tmp_path / "team_totals.parquet"
    pd.DataFrame(_two_team_rows("2026-09-15T22:25:00+00:00", 3, -2.5, 2.5, 44.5)).to_parquet(path)
    monkeypatch.setattr(friday.config, "ODDS_TEAM_TOTALS", path)
    monkeypatch.setattr(friday.odds_store, "read_last_run", lambda job=None: {"job": "line_movement", "stale": False})
    gate = friday.line_movement_read(3)
    assert gate["insufficient"] is True
    assert "only one team_totals capture exists" in gate["reason"]
    assert "2026-09-15" in gate["reason"]


def test_movement_read_success_picks_open_and_current_by_timestamp_not_row_order(tmp_path, monkeypatch):
    """Rows written out of chronological order -- the later capture written
    first -- so this fails if the code sorts by anything but captured_at."""
    path = tmp_path / "team_totals.parquet"
    rows = _two_team_rows("2026-09-19T14:08:00+00:00", 3, -4.0, 4.0, 44.5) + \
        _two_team_rows("2026-09-15T22:25:00+00:00", 3, -2.5, 2.5, 44.5)
    pd.DataFrame(rows).to_parquet(path)
    monkeypatch.setattr(friday.config, "ODDS_TEAM_TOTALS", path)
    monkeypatch.setattr(friday.odds_store, "read_last_run", lambda job=None: {"job": "line_movement", "stale": False})

    gate = friday.line_movement_read(3)

    assert gate["insufficient"] is False
    assert gate["open_at"] == "2026-09-15T22:25:00+00:00"
    assert gate["current_at"] == "2026-09-19T14:08:00+00:00"
    by_team = {r["team"]: r for r in gate["rows"]}
    assert by_team["MIN"]["implied_delta"] == pytest.approx(24.25 - 23.5)
    assert not any("event_id" in r for r in gate["rows"])


# ---- recommended_lineup: un-projected players are excluded, never 0.0 -----

def test_recommended_lineup_excludes_unprojected_players_never_scores_zero():
    rosters_df = pd.DataFrame([
        _roster_row(1, "Projected QB", "QB", "QB", True),
        _roster_row(2, "Unprojected K", "K", "K", True),
    ])
    pool_df = pd.DataFrame([
        _pool_row(1, "Projected QB", "QB", "QB, Bench", 20.0),
        _pool_row(2, "Unprojected K", "K", "K, Bench", None),
    ])
    roster_slots = pd.DataFrame([{"slot": "QB", "count": 1}, {"slot": "K", "count": 1}])
    allowed = {"QB", "K", "Bench", "IR"}

    result = friday.recommended_lineup(rosters_df, pool_df, roster_slots, allowed)

    assert result["insufficient"] is False
    assert "Unprojected K" in result["unprojected_names"]
    assert "K" in result["unfilled_slots"]
    assert all(row["player_name"] != "Unprojected K" for row in result["lineup"])


def test_recommended_lineup_solves_over_projections_not_settled_points():
    """A bench player projected higher than the current starter must be the
    one solve_slots picks -- this report looks forward, unlike tuesday's
    review of settled `points`."""
    rosters_df = pd.DataFrame([
        _roster_row(1, "Weak Starter", "QB", "QB", True),
        _roster_row(2, "Strong Bench", "QB", "Bench", False),
    ])
    pool_df = pd.DataFrame([
        _pool_row(1, "Weak Starter", "QB", "QB, Bench", 10.0),
        _pool_row(2, "Strong Bench", "QB", "QB, Bench", 20.0),
    ])
    roster_slots = pd.DataFrame([{"slot": "QB", "count": 1}])
    allowed = {"QB", "Bench", "IR"}

    result = friday.recommended_lineup(rosters_df, pool_df, roster_slots, allowed)

    assert result["lineup"][0]["player_name"] == "Strong Bench"


# ---- lineup_diff: the slot-name diff, never by instance --------------------

def test_lineup_diff_two_rb_instances_same_players_swapped_order_yields_zero_swap_rows():
    """The spurious-swap trap: solve_slots may return the same two players
    in the opposite instance order from how the current lineup lists them.
    Grouping by slot name must produce zero *changed* rows."""
    starters = pd.DataFrame([
        _roster_row(1, "RB One", "RB", "RB", True),
        _roster_row(2, "RB Two", "RB", "RB", True),
    ])
    pool_df = pd.DataFrame([
        _pool_row(1, "RB One", "RB", "RB, Bench", 10.0),
        _pool_row(2, "RB Two", "RB", "RB, Bench", 12.0),
    ])
    # solve_slots's internal instance order need not match `starters`' row
    # order -- recommended_rows below deliberately lists them reversed.
    recommended_rows = [
        {"slot": "RB", "player_id": 2, "player_name": "RB Two", "projection": 12.0},
        {"slot": "RB", "player_id": 1, "player_name": "RB One", "projection": 10.0},
    ]
    rows = friday.lineup_diff(starters, recommended_rows, pool_df, {}, {}, {})
    assert all(not r["changed"] for r in rows)
    assert {p["player_name"] for r in rows for p in r["current"]} == {"RB One", "RB Two"}


# ---- fired-rule labels ------------------------------------------------------

def _diff_fixture(out_tier=None, out_team="KC", implied_delta_by_team=None, out_proj=10.0, in_proj=13.0):
    starters = pd.DataFrame([_roster_row(1, "Outgoing", "RB", "RB", True, pro_team=out_team)])
    starters["started"] = True
    pool_df = pd.DataFrame([_pool_row(1, "Outgoing", "RB", "RB, Bench", out_proj)])
    recommended_rows = [{"slot": "RB", "player_id": 2, "player_name": "Incoming", "projection": in_proj}]
    avail_by_id = {1: out_tier} if out_tier is not None else {}
    return friday.lineup_diff(
        starters, recommended_rows, pool_df, avail_by_id, implied_delta_by_team or {}, {1: out_team}
    )


def test_rule_projection_gap_fires_at_exactly_the_threshold():
    rows = _diff_fixture(out_proj=10.0, in_proj=10.0 + friday.SWAP_GAP_POINTS)
    assert "projection gap" in rows[0]["reasons"]


def test_rule_projection_gap_does_not_fire_below_threshold():
    rows = _diff_fixture(out_proj=10.0, in_proj=10.0 + friday.SWAP_GAP_POINTS - 0.1)
    assert "projection gap" not in rows[0]["reasons"]


def test_rule_tier_downgrade_fires_for_out_and_high_risk():
    assert "tier downgrade" in _diff_fixture(out_tier="OUT", in_proj=10.5)[0]["reasons"]
    assert "tier downgrade" in _diff_fixture(out_tier="HIGH_RISK", in_proj=10.5)[0]["reasons"]


def test_rule_tier_downgrade_does_not_fire_for_coin_flip():
    """The swap rule is narrower than wednesday.is_at_risk's broader
    watchlist set -- COIN_FLIP feeds the hold-open rule, not this one."""
    rows = _diff_fixture(out_tier="COIN_FLIP", in_proj=10.5)
    assert "tier downgrade" not in rows[0]["reasons"]


def test_rule_team_total_down_fires_on_a_drop():
    rows = _diff_fixture(implied_delta_by_team={"KC": -1.5}, in_proj=10.5)
    assert "team total down" in rows[0]["reasons"]


def test_two_rules_fire_together_and_both_are_rendered():
    rows = _diff_fixture(
        out_tier="OUT", implied_delta_by_team={"KC": -2.0}, out_proj=10.0, in_proj=10.0 + friday.SWAP_GAP_POINTS,
    )
    assert set(rows[0]["reasons"]) == {"projection gap", "tier downgrade", "team total down"}


# ---- held-open slots ---------------------------------------------------------

def _recommended_fixture(candidates, eligibility, values, lineup):
    return {
        "insufficient": False, "candidates": candidates, "eligibility": eligibility, "values": values,
        "lineup": lineup,
    }


def test_held_open_at_risk_starter_with_near_alternative_is_held():
    candidates = [{"player_id": 1, "player_name": "Starter"}, {"player_id": 2, "player_name": "Backup"}]
    eligibility = [{"RB"}, {"RB"}]
    values = [10.0, 9.0]
    lineup = [{"slot": "RB", "player_id": 1, "player_name": "Starter", "projection": 10.0}]
    recommended = _recommended_fixture(candidates, eligibility, values, lineup)
    held = friday.held_open_slots(recommended, {1: "COIN_FLIP"}, {1: "Wed / Thu / Fri"})
    assert len(held) == 1
    assert held[0]["slot"] == "RB"


def test_held_open_at_risk_starter_with_a_wide_edge_over_the_alternative_is_not_held():
    """The recommendation's edge over the next-best legal alternative is
    large -- the solve already made the right call on projection alone, and
    there is nothing to gain by waiting for Sunday's read regardless of risk."""
    candidates = [{"player_id": 1, "player_name": "Starter"}, {"player_id": 2, "player_name": "Backup"}]
    eligibility = [{"RB"}, {"RB"}]
    values = [20.0, 5.0]
    lineup = [{"slot": "RB", "player_id": 1, "player_name": "Starter", "projection": 20.0}]
    recommended = _recommended_fixture(candidates, eligibility, values, lineup)
    held = friday.held_open_slots(recommended, {1: "OUT"}, {1: "Wed / Thu / Fri"})
    assert held == []


def test_held_open_healthy_starter_is_never_held():
    candidates = [{"player_id": 1, "player_name": "Starter"}, {"player_id": 2, "player_name": "Backup"}]
    eligibility = [{"RB"}, {"RB"}]
    values = [10.0, 9.5]
    lineup = [{"slot": "RB", "player_id": 1, "player_name": "Starter", "projection": 10.0}]
    recommended = _recommended_fixture(candidates, eligibility, values, lineup)
    held = friday.held_open_slots(recommended, {1: "CLEAR"}, {1: "Wed / Thu / Fri"})
    assert held == []


def test_held_open_empty_trajectory_and_unresolved_tier_is_held_unconditionally():
    candidates = [{"player_id": 1, "player_name": "Starter"}]
    eligibility = [{"RB"}]
    values = [10.0]
    lineup = [{"slot": "RB", "player_id": 1, "player_name": "Starter", "projection": 10.0}]
    recommended = _recommended_fixture(candidates, eligibility, values, lineup)
    held = friday.held_open_slots(recommended, {}, {1: friday._EMPTY_TRAJECTORY})
    assert len(held) == 1
    assert "unresolved" in held[0]["reason"]


# ---- render: every frame empty produces a complete document ---------------

def test_render_with_every_frame_empty_produces_complete_document():
    text = _render()
    assert "# Lineup lock -- 2026 week 3" in text
    assert "## Decisions due" in text
    assert "## Recommended lineup" in text
    assert "## Bench" in text
    assert "## Line movement since Tuesday's open" in text
    assert "## Practice report -- Wed / Thu / Fri" in text
    assert "## Drop candidates" in text
    assert "## What this report cannot see" in text
    assert friday.INSUFFICIENT_DATA in text


def test_render_never_prints_a_bare_zero_for_an_unprojected_bench_player():
    bench_rows = [{"player_name": "Ghost K", "position": "K", "projection": None, "source": None}]
    text = _render(bench_rows=bench_rows)
    line = next(l for l in text.splitlines() if l.startswith("| Ghost K"))
    assert "0.0" not in line
    assert friday.INSUFFICIENT_DATA in line


# ---- build(): at-risk starter swap names the tier rule, IR never appears --

def test_build_swap_row_names_tier_rule_and_never_recommends_ir(monkeypatch):
    exports = {
        "weekly-rosters": pd.DataFrame([
            _roster_row(1, "Hurt Starter", "RB", "RB", True),
            _roster_row(2, "Good Backup", "RB", "Bench", False),
            _roster_row(3, "Stashed", "RB", "IR", False),
        ]),
        "player-pool": pd.DataFrame([
            _pool_row(1, "Hurt Starter", "RB", "RB, RB/WR, Bench", 8.0),
            _pool_row(2, "Good Backup", "RB", "RB, RB/WR, Bench", 15.0),
            _pool_row(3, "Stashed", "RB", "RB, RB/WR, Bench", 25.0),
        ]),
        "roster-slots": pd.DataFrame([{"slot": "RB", "count": 1}, {"slot": "Bench", "count": 2}, {"slot": "IR", "count": 1}]),
    }
    monkeypatch.setattr(friday, "latest_export", lambda name: exports.get(name, pd.DataFrame()))
    monkeypatch.setattr(
        friday.availability, "read",
        lambda players_df, season, week: pd.DataFrame([
            {"player_id": 1, "tier": "OUT"}, {"player_id": 2, "tier": "CLEAR"}, {"player_id": 3, "tier": "CLEAR"},
        ]),
    )
    monkeypatch.setattr(
        friday.wednesday, "practice_signals",
        lambda players_df, today=None: pd.DataFrame(
            [{"player_id": pid, "practice_trajectory": "DNP / LP / DNP", "matched": True} for pid in [1, 2, 3]]
        ),
    )
    monkeypatch.setattr(friday, "line_movement_read", lambda week: dict(_EMPTY_MOVEMENT))
    monkeypatch.setattr(
        friday.pool, "free_agents",
        lambda week, pool_df=None, rosters_df=None: pd.DataFrame(columns=["player_id", "player_name", "position"]),
    )
    monkeypatch.setattr(friday, "freshness", lambda season=None: {
        "sleeper": (None, True), "nflverse": (None, True), "espn": (None, True), "odds": (None, True),
    })
    monkeypatch.setattr(friday, "espn_export_warning", lambda: None)

    text = friday.build(2026, week=3, team_id=5)

    lineup_section = text.split("## Recommended lineup")[1].split("## Bench")[0]
    assert "tier downgrade" in lineup_section
    assert "Good Backup" in lineup_section
    assert "Stashed" not in lineup_section  # IR is never a lineup-solve candidate
