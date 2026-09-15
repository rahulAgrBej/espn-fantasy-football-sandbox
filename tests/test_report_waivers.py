"""Waiver wire and opening market -- the settlement window, waiver-order
gate, opening-market degradation ladder, and per-slot add candidates.
Fixtures only, no network, no disk."""

import pandas as pd
import pytest

from espn_ff.report import loaders, pool, tuesday, waivers


# ---- fixture builders -------------------------------------------------


def _txn_row(scoring_period, type_, item_type, is_pending=False, bid_amount=0,
             player_name="P", acting_team="T1", execution_type="EXECUTE",
             proposed_date="2026-09-08 12:00:00"):
    return {
        "scoring_period": scoring_period, "type": type_, "item_type": item_type,
        "is_pending": is_pending, "bid_amount": bid_amount, "player_name": player_name,
        "acting_team": acting_team, "execution_type": execution_type, "proposed_date": proposed_date,
    }


def _team_row(team_id, team_name, waiver_rank, wins=0, losses=0, ties=0, points_for=0.0):
    return {
        "team_id": team_id, "team_name": team_name, "waiver_rank": waiver_rank,
        "wins": wins, "losses": losses, "ties": ties, "points_for": points_for,
    }


def _pool_row(player_id, player_name, position, pro_team, eligible_slots, week_projected,
              percent_owned=10.0, week=2, injury_status="ACTIVE"):
    return {
        "player_id": player_id, "player_name": player_name, "position": position,
        "pro_team": pro_team, "active": True, "injured": False, "injury_status": injury_status,
        "on_team_id": 0, "percent_owned": percent_owned, "percent_started": percent_owned,
        "eligible_slots": eligible_slots, "season_points": 0.0, "season_projected": 0.0,
        "week": week, "week_points": None, "week_projected": week_projected,
    }


def _roster_row(player_id, player_name, position, lineup_slot, started, projected=None,
                 week=2, team_id=5, injury_status="ACTIVE"):
    return {
        "season": 2026, "week": week, "team_id": team_id, "team_name": "Us",
        "player_id": player_id, "player_name": player_name, "position": position,
        "pro_team": "FA", "lineup_slot_id": 0, "lineup_slot": lineup_slot, "started": started,
        "injury_status": injury_status, "acquisition_type": "DRAFT", "points": None, "projected": projected,
    }


_ROSTER_SLOTS = pd.DataFrame(
    [
        {"slot": "QB", "count": 1}, {"slot": "RB", "count": 1}, {"slot": "RB/WR", "count": 1},
        {"slot": "WR", "count": 1}, {"slot": "TE", "count": 1}, {"slot": "D/ST", "count": 1},
        {"slot": "K", "count": 1}, {"slot": "Bench", "count": 3}, {"slot": "IR", "count": 1},
    ]
)
_ALLOWED_SLOTS = monday_slots = tuesday.monday.league_slots(_ROSTER_SLOTS)


# ---- settlements --------------------------------------------------------


def test_empty_transactions_export_renders_insufficient_not_an_empty_table():
    result = waivers.settlements(pd.DataFrame(), week=2)
    assert result["insufficient"] is True


def test_pending_claim_is_not_reported_as_settled():
    df = pd.DataFrame([_txn_row(2, "FREEAGENT", "ADD", is_pending=True)])
    result = waivers.settlements(df, week=2)
    assert result["insufficient"] is False
    assert result["pending_count"] == 1
    assert result["rows"][0]["is_pending"] is True


def test_all_zero_bids_report_as_no_faab_evidence_not_zero_spend():
    df = pd.DataFrame(
        [_txn_row(1, "FREEAGENT", "ADD", bid_amount=0), _txn_row(1, "FREEAGENT", "DROP", bid_amount=0)]
    )
    result = waivers.settlements(df, week=2)
    assert result["faab_in_use"] is False
    assert result["waiver_type_observed"] is False


def test_waiver_type_observed_when_a_waiver_row_exists_anywhere_in_the_export():
    df = pd.DataFrame(
        [_txn_row(1, "DRAFT", "DRAFT"), _txn_row(3, "WAIVER", "ADD", bid_amount=5)]
    )
    result = waivers.settlements(df, week=2)
    assert result["waiver_type_observed"] is True
    assert result["faab_in_use"] is False  # week window here is {1,2}; the WAIVER row is scoring_period 3


def test_non_settlement_types_are_excluded_and_counted():
    df = pd.DataFrame(
        [_txn_row(2, "DRAFT", "DRAFT"), _txn_row(2, "TRADE_PROPOSAL", "TRADE"), _txn_row(2, "FREEAGENT", "ADD")]
    )
    result = waivers.settlements(df, week=2)
    assert len(result["rows"]) == 1
    assert result["excluded_count"] == 2


# ---- waiver order ---------------------------------------------------------


def test_waiver_order_survives_the_preseason_zero_zero_snapshot():
    """tuesday.standings() flags an all-0-0-0 snapshot as insufficient;
    waiver_order must not inherit that gate -- waiver_rank is reverse draft
    order and is meaningful from week 1."""
    teams_df = pd.DataFrame([_team_row(i, f"T{i}", i) for i in range(1, 13)])
    assert tuesday.standings(teams_df, week=1)["insufficient"] is True

    result = waivers.waiver_order(teams_df, team_id=5)
    assert result["insufficient"] is False
    assert result["our_rank"] == 5
    assert result["our_rank_of"] == 12


def test_waiver_order_insufficient_when_column_absent():
    teams_df = pd.DataFrame([{"team_id": 1, "team_name": "T1"}])
    result = waivers.waiver_order(teams_df, team_id=1)
    assert result["insufficient"] is True


def test_waiver_order_insufficient_when_column_all_null():
    teams_df = pd.DataFrame([{"team_id": 1, "team_name": "T1", "waiver_rank": None}])
    result = waivers.waiver_order(teams_df, team_id=1)
    assert result["insufficient"] is True


# ---- team totals / opening market -----------------------------------------


def test_missing_odds_renders_insufficient_data_not_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(waivers.config, "ODDS_TEAM_TOTALS", tmp_path / "team_totals.parquet")
    result = waivers.team_totals(week=2)
    assert result["insufficient"] is True
    assert result["by_team"] == {}


def test_stale_slate_last_run_is_treated_as_missing_odds(tmp_path, monkeypatch):
    parquet_path = tmp_path / "team_totals.parquet"
    pd.DataFrame(
        [{"event_id": "a" * 32, "team": "SEA", "spread": -3.0, "total": 44.0, "implied_team_total": 23.5,
          "captured_at": "2026-09-15T09:00:00+00:00"}]
    ).to_parquet(parquet_path)
    monkeypatch.setattr(waivers.config, "ODDS_TEAM_TOTALS", parquet_path)
    monkeypatch.setattr(
        waivers.odds_store, "read_last_run",
        lambda job=None: {"job": "slate_context", "stale": True, "reason": "budget aborted"},
    )
    result = waivers.team_totals(week=2)
    assert result["insufficient"] is True
    assert "budget aborted" in result["reason"]


def test_event_id_never_reaches_the_by_team_dict(tmp_path, monkeypatch):
    parquet_path = tmp_path / "team_totals.parquet"
    pd.DataFrame(
        [{"event_id": "a" * 32, "team": "SEA", "spread": -3.0, "total": 44.0, "implied_team_total": 23.5,
          "captured_at": "2026-09-15T09:00:00+00:00"}]
    ).to_parquet(parquet_path)
    monkeypatch.setattr(waivers.config, "ODDS_TEAM_TOTALS", parquet_path)
    monkeypatch.setattr(waivers.odds_store, "read_last_run", lambda job=None: {"job": "slate_context", "stale": False})
    monkeypatch.setattr(
        waivers.odds_projections, "build",
        lambda week=None: (pd.DataFrame(), pd.DataFrame([{"team": "SEA", "implied_team_total": 23.5}])),
    )
    result = waivers.team_totals(week=2)
    assert result["insufficient"] is False
    assert result["by_team"] == {"SEA": 23.5}
    assert "event_id" not in str(result["by_team"])


# ---- add candidates ---------------------------------------------------------


def _free_agents_and_rosters():
    pool_df = pd.DataFrame(
        [
            _pool_row("bench_rb", "Bench RB", "RB", "DAL", "RB,RB/WR,Bench,IR", 5.0),
            _pool_row("fa_rb_hi", "FA RB Hi", "RB", "SEA", "RB,RB/WR,Bench,IR", 12.0),
            _pool_row("fa_rb_lo", "FA RB Lo", "RB", "NYJ", "RB,RB/WR,Bench,IR", 3.0),
            _pool_row("fa_nan", "FA NaN", "RB", "MIA", "RB,RB/WR,Bench,IR", float("nan")),
        ]
    )
    rosters_df = pd.DataFrame(
        [
            _roster_row("starter_rb", "Starter RB", "RB", "RB", True),
            _roster_row("bench_rb", "Bench RB", "RB", "Bench", False),
        ]
    )
    return pool_df, rosters_df


def test_on_team_id_is_never_read_for_availability():
    pool_df, rosters_df = _free_agents_and_rosters()
    # Every pool row -- including a rostered one -- carries on_team_id = 0.
    assert (pool_df["on_team_id"] == 0).all()
    free_agents_df = pool.free_agents(2, pool_df=pool_df, rosters_df=rosters_df)
    names = set(free_agents_df["player_name"])
    assert "Bench RB" not in names
    assert "FA RB Hi" in names


def test_nan_projection_is_named_not_silently_dropped():
    pool_df, rosters_df = _free_agents_and_rosters()
    free_agents_df = pool.free_agents(2, pool_df=pool_df, rosters_df=rosters_df)
    blocks, _, nan_names, _ = waivers.add_candidates(
        free_agents_df, rosters_df, week=2, team_id=5, pool_df=pool_df,
        roster_slots_df=_ROSTER_SLOTS, allowed_slots=_ALLOWED_SLOTS,
    )
    assert "FA NaN" in nan_names
    rb_block = next(b for b in blocks if b["slot"] == "RB")
    assert "FA NaN" not in {r["player_name"] for r in rb_block["rows"]}


def test_slot_with_no_eligible_bench_floor_renders_insufficient_gap():
    """No K on the bench -- the K block must show a None bench_floor
    (renders as insufficient data) with the current starter as context,
    never a substituted zero."""
    pool_df = pd.DataFrame(
        [
            _pool_row("fa_k", "FA Kicker", "K", "SEA", "K,Bench,IR", 8.0),
        ]
    )
    rosters_df = pd.DataFrame(
        [_roster_row("starter_k", "Starter K", "K", "K", True, projected=7.0)]
    )
    free_agents_df = pool.free_agents(2, pool_df=pool_df, rosters_df=rosters_df)
    blocks, *_ = waivers.add_candidates(
        free_agents_df, rosters_df, week=2, team_id=5, pool_df=pool_df,
        roster_slots_df=_ROSTER_SLOTS, allowed_slots=_ALLOWED_SLOTS,
    )
    k_block = next(b for b in blocks if b["slot"] == "K")
    assert k_block["bench_floor"] is None
    assert k_block["starter_context"]["player_name"] == "Starter K"
    assert k_block["rows"][0]["gap"] is None


def test_wr_and_rb_wr_blocks_may_repeat_a_candidate():
    pool_df = pd.DataFrame(
        [_pool_row("fa_wr", "FA Wideout", "WR", "SEA", "RB/WR,WR,Bench,IR", 10.0)]
    )
    rosters_df = pd.DataFrame([_roster_row("bench_wr", "Bench WR", "WR", "Bench", False)])
    # give the bench WR a pool row too, so a floor exists
    pool_df = pd.concat(
        [pool_df, pd.DataFrame([_pool_row("bench_wr", "Bench WR", "WR", "DAL", "RB/WR,WR,Bench,IR", 4.0)])],
        ignore_index=True,
    )
    free_agents_df = pool.free_agents(2, pool_df=pool_df, rosters_df=rosters_df)
    blocks, *_ = waivers.add_candidates(
        free_agents_df, rosters_df, week=2, team_id=5, pool_df=pool_df,
        roster_slots_df=_ROSTER_SLOTS, allowed_slots=_ALLOWED_SLOTS,
    )
    wr_names = {r["player_name"] for b in blocks if b["slot"] == "WR" for r in b["rows"]}
    rbwr_names = {r["player_name"] for b in blocks if b["slot"] == "RB/WR" for r in b["rows"]}
    assert wr_names == rbwr_names == {"FA Wideout"}


def test_blocks_are_ordered_most_constrained_first():
    pool_df = pd.DataFrame(
        [
            _pool_row("fa_qb", "FA QB", "QB", "SEA", "QB,Bench,IR", 15.0),
            _pool_row("fa_rb1", "FA RB1", "RB", "SEA", "RB,RB/WR,Bench,IR", 10.0),
            _pool_row("fa_rb2", "FA RB2", "RB", "DAL", "RB,RB/WR,Bench,IR", 9.0),
        ]
    )
    rosters_df = pd.DataFrame([_roster_row("bench_rb", "Bench RB", "RB", "Bench", False)])
    pool_df = pd.concat(
        [pool_df, pd.DataFrame([_pool_row("bench_rb", "Bench RB", "RB", "DAL", "RB,RB/WR,Bench,IR", 2.0)])],
        ignore_index=True,
    )
    free_agents_df = pool.free_agents(2, pool_df=pool_df, rosters_df=rosters_df)
    blocks, *_ = waivers.add_candidates(
        free_agents_df, rosters_df, week=2, team_id=5, pool_df=pool_df,
        roster_slots_df=_ROSTER_SLOTS, allowed_slots=_ALLOWED_SLOTS,
    )
    # QB has 1 eligible free agent, RB has 2 -- QB must come first.
    slots_in_order = [b["slot"] for b in blocks]
    assert slots_in_order.index("QB") < slots_in_order.index("RB")


def test_trending_add_does_not_change_the_ordering():
    pool_df, rosters_df = _free_agents_and_rosters()
    free_agents_df = pool.free_agents(2, pool_df=pool_df, rosters_df=rosters_df)
    blocks_a, *_ = waivers.add_candidates(
        free_agents_df, rosters_df, week=2, team_id=5, pool_df=pool_df,
        roster_slots_df=_ROSTER_SLOTS, allowed_slots=_ALLOWED_SLOTS,
    )
    flipped = free_agents_df.copy()
    flipped["trending_add"] = [True] * len(flipped)
    blocks_b, *_ = waivers.add_candidates(
        flipped, rosters_df, week=2, team_id=5, pool_df=pool_df,
        roster_slots_df=_ROSTER_SLOTS, allowed_slots=_ALLOWED_SLOTS,
    )
    order_a = [r["player_name"] for b in blocks_a for r in b["rows"]]
    order_b = [r["player_name"] for b in blocks_b for r in b["rows"]]
    assert order_a == order_b


def test_position_fallback_is_named_when_a_candidate_has_no_pool_row():
    # A free agent with no pool row at all falls back to _POSITION_SLOTS.
    pool_df = pd.DataFrame([_pool_row("fa_rb", "FA RB", "RB", "SEA", "RB,RB/WR,Bench,IR", 10.0)])
    rosters_df = pd.DataFrame()
    free_agents_df = pool_df.copy()
    # Simulate "dropped since export" by blanking eligible_slots so the
    # primary path returns nothing and the fallback path must fire.
    free_agents_df.loc[free_agents_df["player_id"] == "fa_rb", "eligible_slots"] = ""
    blocks, fallback_names, *_ = waivers.add_candidates(
        free_agents_df, rosters_df, week=2, team_id=5, pool_df=pd.DataFrame(columns=pool_df.columns),
        roster_slots_df=_ROSTER_SLOTS, allowed_slots=_ALLOWED_SLOTS,
    )
    assert "FA RB" in fallback_names


# ---- pair_drops -----------------------------------------------------------


def test_sole_one_deep_backup_is_never_offered_as_a_drop():
    rosters_df = pd.DataFrame(
        [
            _roster_row("qb1", "Starter QB", "QB", "QB", True),
            _roster_row("qb2", "Backup QB", "QB", "Bench", False),
            _roster_row("wr1", "Bench WR", "WR", "Bench", False),
        ]
    )
    pool_df = pd.DataFrame(columns=["player_id", "season_points", "season_projected"])
    drop_list = tuesday.drop_candidates(rosters_df, pool_df, week=2, team_id=5)
    names = [d["player_name"] for d in drop_list]
    assert "Backup QB" not in names
    assert "Bench WR" in names


def test_drops_are_distinct_within_a_slot_block_and_reused_across_blocks():
    drop_list = [{"player_name": f"Drop{i}", "position": "WR", "ros_projection": i} for i in range(2)]
    blocks = [
        {"slot": "RB", "rows": [{"drop_name": None} for _ in range(3)]},
        {"slot": "WR", "rows": [{"drop_name": None} for _ in range(2)]},
    ]
    paired = waivers.pair_drops(blocks, drop_list)
    rb_drops = [r["drop_name"] for r in paired[0]["rows"]]
    assert rb_drops[0] != rb_drops[1]  # distinct within the block
    assert rb_drops == ["Drop0", "Drop1", "Drop0"]  # wraps when rows exceed the pool
    wr_drops = [r["drop_name"] for r in paired[1]["rows"]]
    assert wr_drops == rb_drops[:2]  # reused across blocks


def test_pair_drops_with_no_legal_drop_leaves_rows_undropped():
    blocks = [{"slot": "K", "rows": [{"drop_name": None}]}]
    paired = waivers.pair_drops(blocks, [])
    assert paired[0]["rows"][0]["drop_name"] is None


# ---- render assembly -------------------------------------------------------


def test_render_never_emits_a_32_hex_event_id():
    settle = {"insufficient": True, "reason": "no transactions export on disk"}
    order = {"insufficient": True, "reason": "no usable waiver_rank column"}
    totals = {"insufficient": True, "reason": "no team_totals.parquet on disk", "by_team": {}}
    text = waivers.render(2026, 2, 5, settle, order, [], totals, [], list(waivers.FOOTER_NOTES))
    import re
    assert not re.search(r"(^|[^0-9a-fA-F])[0-9a-fA-F]{32}([^0-9a-fA-F]|$)", text)
    assert "# Waiver wire and opening market -- 2026 week 2" in text
    assert text.index("## Decisions due") < text.index("## Waiver settlements")
    assert text.index("## What this report cannot see") > text.index("## Add candidates")


# ---- loaders: odds freshness -----------------------------------------------


def test_odds_freshness_prefers_captured_at_over_ran_at(tmp_path, monkeypatch):
    """A budget-aborted run writes a fresh `ran_at` over an unchanged
    snapshot -- the freshness figure must reflect the stale snapshot's own
    captured_at, not the misleadingly-fresh ran_at."""
    parquet_path = tmp_path / "team_totals.parquet"
    old_captured = pd.Timestamp("2026-09-01T09:00:00+00:00")
    pd.DataFrame(
        [{"event_id": "a" * 32, "team": "SEA", "captured_at": old_captured.isoformat()}]
    ).to_parquet(parquet_path)
    monkeypatch.setattr(loaders.config, "ODDS_TEAM_TOTALS", parquet_path)
    monkeypatch.setattr(
        loaders.odds_store, "read_last_run",
        lambda job=None: {"job": "slate_context", "ran_at": pd.Timestamp("2026-09-15T09:00:00+00:00").timestamp(),
                           "stale": False},
    )
    ts, stale = loaders._odds_freshness()
    assert ts == pytest.approx(old_captured.timestamp())
    assert stale is False


def test_odds_freshness_absent_last_run_and_parquet_reads_never_and_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(loaders.config, "ODDS_TEAM_TOTALS", tmp_path / "team_totals.parquet")
    monkeypatch.setattr(loaders.odds_store, "read_last_run", lambda job=None: None)
    ts, stale = loaders._odds_freshness()
    assert ts is None
    assert stale is True


def test_odds_freshness_keys_on_slate_context_specifically():
    """freshness() must never mask slate_context's staleness with another
    job's -- docs/report-weekly-schedule.md:336-341's per-job invariant."""
    captured = {}

    def fake_read_last_run(job=None):
        captured["job"] = job
        return None

    import espn_ff.report.loaders as loaders_module
    orig = loaders_module.odds_store.read_last_run
    loaders_module.odds_store.read_last_run = fake_read_last_run
    try:
        loaders_module._odds_freshness()
    finally:
        loaders_module.odds_store.read_last_run = orig
    assert captured["job"] == "slate_context"


def test_freshness_lines_renders_all_four_feeds_including_odds():
    from espn_ff.report.render import freshness_lines

    fresh = {"sleeper": (None, True), "nflverse": (None, True), "espn": (None, True), "odds": (None, True)}
    lines = freshness_lines(fresh)
    assert any(line.startswith("- odds:") for line in lines)


def test_freshness_lines_tolerates_a_partial_dict():
    from espn_ff.report.render import freshness_lines

    fresh = {"sleeper": (None, True), "nflverse": (None, True), "espn": (None, True)}
    lines = freshness_lines(fresh)
    assert len(lines) == 3
