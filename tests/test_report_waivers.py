"""Waiver wire and opening market -- the settlement window, waiver-order
gate, opening-market degradation ladder, and per-slot add candidates.
Fixtures only, no network, no disk."""

import json
from datetime import datetime

import pandas as pd
import pytest

from espn_ff.report import loaders, pool, tuesday, waivers
from espn_ff.weeks import ET


# ---- fixture builders -------------------------------------------------


def _txn_row(scoring_period, type_, item_type, is_pending=False, bid_amount=0,
             player_name="P", acting_team="T1", execution_type="EXECUTE",
             proposed_date="2026-09-08 12:00:00", player_id=1, acting_team_id=1, transaction_id=100,
             status="EXECUTED"):
    return {
        "scoring_period": scoring_period, "type": type_, "item_type": item_type,
        "is_pending": is_pending, "bid_amount": bid_amount, "player_name": player_name,
        "acting_team": acting_team, "execution_type": execution_type, "proposed_date": proposed_date,
        "player_id": player_id, "acting_team_id": acting_team_id, "transaction_id": transaction_id,
        "status": status,
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


# ---- waiver outcomes -------------------------------------------------------


def test_waiver_outcomes_empty_transactions_export_renders_insufficient():
    free_agents_df = pd.DataFrame(columns=["player_id"])
    result = waivers.waiver_outcomes(pd.DataFrame(), pd.DataFrame(), free_agents_df, week=2, team_id=5)
    assert result["insufficient"] is True


def test_waiver_outcomes_add_attributed_by_acting_team_id_not_the_display_string():
    # acting_team's display string deliberately does not match team_id 5's real name --
    # attribution must come from the numeric acting_team_id, never the string.
    df = pd.DataFrame([
        _txn_row(2, "FREEAGENT", "ADD", player_id=10, acting_team_id=5, acting_team="Some Other Team Name"),
    ])
    pool_df = pd.DataFrame([_pool_row(10, "Our Claim", "RB", "SEA", "RB,RB/WR,Bench,IR", 8.0)])
    free_agents_df = pd.DataFrame(columns=["player_id"])
    result = waivers.waiver_outcomes(df, pool_df, free_agents_df, week=2, team_id=5)
    assert [r["player_name"] for r in result["claimed_by_us"]] == ["Our Claim"]
    assert result["claimed_by_others"] == []


def test_waiver_outcomes_add_attributed_to_another_team():
    df = pd.DataFrame([_txn_row(2, "FREEAGENT", "ADD", player_id=10, acting_team_id=3, acting_team="Rival")])
    pool_df = pd.DataFrame([_pool_row(10, "Rival Claim", "RB", "SEA", "RB,RB/WR,Bench,IR", 8.0)])
    free_agents_df = pd.DataFrame(columns=["player_id"])
    result = waivers.waiver_outcomes(df, pool_df, free_agents_df, week=2, team_id=5)
    assert result["claimed_by_us"] == []
    assert [r["player_name"] for r in result["claimed_by_others"]] == ["Rival Claim"]
    assert result["claimed_by_others"][0]["acting_team"] == "Rival"


def test_waiver_outcomes_drop_row_nan_player_name_resolved_via_pool_df():
    df = pd.DataFrame([_txn_row(2, "FREEAGENT", "DROP", player_id=20, acting_team_id=3,
                                 player_name=float("nan"))])
    pool_df = pd.DataFrame([_pool_row(20, "Resolved Name", "WR", "DAL", "WR,RB/WR,Bench,IR", 4.0)])
    free_agents_df = pd.DataFrame([{"player_id": 20}])
    result = waivers.waiver_outcomes(df, pool_df, free_agents_df, week=2, team_id=5)
    assert [r["player_name"] for r in result["newly_available"]] == ["Resolved Name"]


def test_waiver_outcomes_drop_present_in_free_agents_is_newly_available():
    df = pd.DataFrame([_txn_row(2, "FREEAGENT", "DROP", player_id=20, acting_team_id=3)])
    pool_df = pd.DataFrame([_pool_row(20, "Dropped Player", "WR", "DAL", "WR,RB/WR,Bench,IR", 4.0)])
    free_agents_df = pd.DataFrame([{"player_id": 20}])
    result = waivers.waiver_outcomes(df, pool_df, free_agents_df, week=2, team_id=5)
    assert [r["player_name"] for r in result["newly_available"]] == ["Dropped Player"]


def test_waiver_outcomes_drop_absent_from_free_agents_is_excluded_as_reclaimed():
    df = pd.DataFrame([_txn_row(2, "FREEAGENT", "DROP", player_id=20, acting_team_id=3)])
    pool_df = pd.DataFrame([_pool_row(20, "Reclaimed Player", "WR", "DAL", "WR,RB/WR,Bench,IR", 4.0)])
    free_agents_df = pd.DataFrame(columns=["player_id"])  # not in this week's free-agent pool
    result = waivers.waiver_outcomes(df, pool_df, free_agents_df, week=2, team_id=5)
    assert result["newly_available"] == []


def test_waiver_outcomes_pending_row_excluded_and_counted():
    df = pd.DataFrame([
        _txn_row(2, "FREEAGENT", "ADD", player_id=10, acting_team_id=5, is_pending=True),
        _txn_row(2, "FREEAGENT", "ADD", player_id=11, acting_team_id=5, is_pending=False),
    ])
    pool_df = pd.DataFrame([
        _pool_row(10, "Pending Claim", "RB", "SEA", "RB,RB/WR,Bench,IR", 8.0),
        _pool_row(11, "Settled Claim", "RB", "SEA", "RB,RB/WR,Bench,IR", 8.0),
    ])
    free_agents_df = pd.DataFrame(columns=["player_id"])
    result = waivers.waiver_outcomes(df, pool_df, free_agents_df, week=2, team_id=5)
    assert result["pending_count"] == 1
    assert [r["player_name"] for r in result["claimed_by_us"]] == ["Settled Claim"]


def test_waiver_outcomes_week_one_edge_case_does_not_look_up_week_zero():
    df = pd.DataFrame([_txn_row(1, "FREEAGENT", "ADD", player_id=10, acting_team_id=5)])
    pool_df = pd.DataFrame([_pool_row(10, "Week One Claim", "RB", "SEA", "RB,RB/WR,Bench,IR", 8.0)])
    free_agents_df = pd.DataFrame(columns=["player_id"])
    result = waivers.waiver_outcomes(df, pool_df, free_agents_df, week=1, team_id=5)
    assert result["insufficient"] is False
    assert [r["player_name"] for r in result["claimed_by_us"]] == ["Week One Claim"]


def test_waiver_outcomes_paired_add_drop_rows_resolved_independently():
    df = pd.DataFrame([
        _txn_row(2, "FREEAGENT", "ADD", player_id=30, acting_team_id=5, transaction_id=500),
        _txn_row(2, "FREEAGENT", "DROP", player_id=31, acting_team_id=5, transaction_id=500),
    ])
    pool_df = pd.DataFrame([
        _pool_row(30, "Added Player", "RB", "SEA", "RB,RB/WR,Bench,IR", 8.0),
        _pool_row(31, "Dropped Player", "WR", "DAL", "WR,RB/WR,Bench,IR", 4.0),
    ])
    free_agents_df = pd.DataFrame([{"player_id": 31}])
    result = waivers.waiver_outcomes(df, pool_df, free_agents_df, week=2, team_id=5)
    assert [r["player_name"] for r in result["claimed_by_us"]] == ["Added Player"]
    assert [r["player_name"] for r in result["newly_available"]] == ["Dropped Player"]


def test_failed_claim_by_us_is_never_reported_as_claimed_by_us():
    """The 2026-09-16 regression: ESPN records every team's attempt on a
    contested player -- one EXECUTED for the winner, a FAILED_* for each
    loser, at the same timestamp. Filtering on type/item_type alone reported
    a player we LOST as claimed by us, and showed him claimed by two teams
    at once."""
    df = pd.DataFrame([
        _txn_row(2, "WAIVER", "ADD", player_id=10, acting_team_id=8, acting_team="Rival",
                 status="EXECUTED", transaction_id=200),
        _txn_row(2, "WAIVER", "ADD", player_id=10, acting_team_id=5, acting_team="Us",
                 status="FAILED_INVALIDPLAYERSOURCE", transaction_id=201),
    ])
    pool_df = pd.DataFrame([_pool_row(10, "Contested Guy", "WR", "NO", "WR,RB/WR,Bench,IR", 9.0)])
    result = waivers.waiver_outcomes(df, pool_df, pd.DataFrame(columns=["player_id"]), week=2, team_id=5)

    assert result["claimed_by_us"] == []
    assert [r["player_name"] for r in result["claimed_by_others"]] == ["Contested Guy"]
    assert result["failed_count"] == 1


def test_failed_drop_does_not_make_a_player_newly_available():
    df = pd.DataFrame([_txn_row(2, "WAIVER", "DROP", player_id=20, acting_team_id=5,
                                 status="FAILED_INVALIDPLAYERSOURCE")])
    pool_df = pd.DataFrame([_pool_row(20, "Still Rostered", "WR", "DAL", "WR,Bench,IR", 4.0)])
    free_agents_df = pd.DataFrame([{"player_id": 20}])
    result = waivers.waiver_outcomes(df, pool_df, free_agents_df, week=2, team_id=5)
    assert result["newly_available"] == []
    assert result["failed_count"] == 1


@pytest.mark.parametrize("status", ["FAILED_ROSTERLIMIT", "FAILED_INVALIDPLAYERSOURCE", "CANCELED"])
def test_every_unsuccessful_status_is_counted_never_silently_dropped(status):
    df = pd.DataFrame([_txn_row(2, "WAIVER", "ADD", player_id=10, acting_team_id=5, status=status)])
    pool_df = pd.DataFrame([_pool_row(10, "P", "WR", "NO", "WR,Bench,IR", 9.0)])
    result = waivers.waiver_outcomes(df, pool_df, pd.DataFrame(columns=["player_id"]), week=2, team_id=5)
    assert result["claimed_by_us"] == [] and result["claimed_by_others"] == []
    assert result["failed_count"] == 1


def test_null_is_pending_and_null_status_are_counted_not_dropped():
    """The three buckets must be exhaustive -- a row with neither field
    populated previously fell through `== True` and `== False` alike and
    vanished from both the pending count and the settled set."""
    df = pd.DataFrame([_txn_row(2, "WAIVER", "ADD", player_id=10, acting_team_id=5,
                                 is_pending=None, status=None)])
    pool_df = pd.DataFrame([_pool_row(10, "Ghost", "WR", "NO", "WR,Bench,IR", 9.0)])
    result = waivers.waiver_outcomes(df, pool_df, pd.DataFrame(columns=["player_id"]), week=2, team_id=5)
    assert result["unknown_count"] == 1
    assert result["pending_count"] == 0 and result["failed_count"] == 0
    assert result["claimed_by_us"] == []


def test_waiver_outcomes_unresolved_player_id_surfaced_without_crashing():
    df = pd.DataFrame([_txn_row(2, "FREEAGENT", "ADD", player_id=999, acting_team_id=5)])
    pool_df = pd.DataFrame(columns=["player_id", "player_name", "position", "pro_team"])
    free_agents_df = pd.DataFrame(columns=["player_id"])
    result = waivers.waiver_outcomes(df, pool_df, free_agents_df, week=2, team_id=5)
    assert 999 in result["unresolved_ids"]
    assert len(result["claimed_by_us"]) == 1


# ---- the settled-read gate -------------------------------------------------


def _et(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=ET).timestamp()


@pytest.mark.parametrize("fetched,rendered,expected", [
    # Wed 09:08 pull, Wed 10:00 render -- the intended shape.
    (_et(2026, 9, 16, 9, 8), _et(2026, 9, 16, 10, 0), True),
    # The 2026-09-16 incident: Tue 14:10 pull, Wed 10:03 render. ~20h old, so
    # it passed the flat 24-hour ESPN_STALE_HOURS check while predating the
    # waiver run entirely.
    (_et(2026, 9, 15, 14, 10), _et(2026, 9, 16, 10, 3), False),
    # A Wednesday-afternoon re-render off the morning pull is still settled.
    (_et(2026, 9, 16, 9, 8), _et(2026, 9, 16, 16, 30), True),
    # A Friday render still reading Tuesday's pull is not.
    (_et(2026, 9, 15, 14, 10), _et(2026, 9, 18, 10, 0), False),
    # A Friday render off Wednesday's pull is.
    (_et(2026, 9, 16, 9, 8), _et(2026, 9, 18, 10, 0), True),
])
def test_waiver_read_is_settled_uses_the_run_boundary_not_an_age_threshold(fetched, rendered, expected):
    settled, reason = waivers.waiver_read_is_settled(fetched, rendered)
    assert settled is expected
    assert (reason is None) is expected


def test_never_fetched_is_not_a_settled_read():
    settled, reason = waivers.waiver_read_is_settled(None, _et(2026, 9, 16, 10, 0))
    assert settled is False and "never been fetched" in reason


def test_gate_failure_returns_insufficient_rather_than_zeros():
    """A pre-settlement read must not render as '0 claimed' -- that is a
    misleading figure, not a finding."""
    df = pd.DataFrame([_txn_row(2, "WAIVER", "ADD", player_id=10, acting_team_id=8)])
    pool_df = pd.DataFrame([_pool_row(10, "Someone", "WR", "NO", "WR,Bench,IR", 9.0)])
    result = waivers.waiver_outcomes(
        df, pool_df, pd.DataFrame(columns=["player_id"]), week=2, team_id=5,
        settled_read=(False, "the ESPN transactions payload was fetched Tue ..."),
    )
    assert result["insufficient"] is True
    assert "fetched Tue" in result["reason"]
    assert result["claimed_by_others"] == []


def test_gate_pass_does_not_suppress_real_outcomes():
    df = pd.DataFrame([_txn_row(2, "WAIVER", "ADD", player_id=10, acting_team_id=8)])
    pool_df = pd.DataFrame([_pool_row(10, "Someone", "WR", "NO", "WR,Bench,IR", 9.0)])
    result = waivers.waiver_outcomes(
        df, pool_df, pd.DataFrame(columns=["player_id"]), week=2, team_id=5,
        settled_read=(True, None),
    )
    assert result["insufficient"] is False
    assert [r["player_name"] for r in result["claimed_by_others"]] == ["Someone"]


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


_FIXED_RENDERED_AT = 1789504260  # Tue 2026-09-15 16:31 ET


def test_render_never_emits_a_32_hex_event_id():
    settle = {"insufficient": True, "reason": "no transactions export on disk"}
    order = {"insufficient": True, "reason": "no usable waiver_rank column"}
    totals = {"insufficient": True, "reason": "no team_totals.parquet on disk", "by_team": {}}
    text = waivers.render(
        2026, 2, 5, settle, order, [], totals, [], list(waivers.FOOTER_NOTES),
        rendered_at=_FIXED_RENDERED_AT,
    )
    import re
    assert not re.search(r"(^|[^0-9a-fA-F])[0-9a-fA-F]{32}([^0-9a-fA-F]|$)", text)
    assert "# Waiver wire and opening market -- 2026 week 2" in text
    assert text.index("## Decisions due") < text.index("## Waiver settlements")
    assert text.index("## What this report cannot see") > text.index("## Add candidates")


def test_render_header_states_the_covered_window_and_render_time():
    settle = {"insufficient": True, "reason": "no transactions export on disk"}
    order = {"insufficient": True, "reason": "no usable waiver_rank column"}
    totals = {"insufficient": True, "reason": "no team_totals.parquet on disk", "by_team": {}}
    window = (datetime(2026, 9, 15, 3, 0, tzinfo=ET), datetime(2026, 9, 22, 3, 0, tzinfo=ET))
    prev_window = (datetime(2026, 9, 8, 3, 0, tzinfo=ET), datetime(2026, 9, 15, 3, 0, tzinfo=ET))
    text = waivers.render(
        2026, 2, 5, settle, order, [], totals, [], list(waivers.FOOTER_NOTES),
        window=window, prev_window=prev_window, rendered_at=_FIXED_RENDERED_AT,
    )
    assert "**Week 2** Tue 2026-09-15 03:00 - Tue 2026-09-22 03:00 ET" in text
    assert "**Rendered** Tue 2026-09-15 16:31 ET" in text
    assert "week 1" in text.split("**Covers**")[1].splitlines()[0]


def test_render_header_shows_insufficient_data_when_the_calendar_is_absent():
    settle = {"insufficient": True, "reason": "no transactions export on disk"}
    order = {"insufficient": True, "reason": "no usable waiver_rank column"}
    totals = {"insufficient": True, "reason": "no team_totals.parquet on disk", "by_team": {}}
    text = waivers.render(
        2026, 2, 5, settle, order, [], totals, [], list(waivers.FOOTER_NOTES),
        rendered_at=_FIXED_RENDERED_AT,
    )
    assert "**Week 2** insufficient data" in text


# ---- loaders: odds freshness -----------------------------------------------


def test_espn_view_freshness_does_not_let_a_fresh_view_mask_a_stale_one(tmp_path, monkeypatch):
    """The silence behind the 2026-09-16 incident: `_espn_freshness` takes
    the max across every cached view, so a fresh mMatchupScore pull made a
    day-old mTransactions2 payload read as fresh and no staleness warning
    ever appeared."""
    from espn_ff import cache

    season_dir = tmp_path / "2026"
    season_dir.mkdir()
    old = datetime(2026, 9, 15, 14, 10, tzinfo=ET).timestamp()
    new = datetime(2026, 9, 16, 9, 24, tzinfo=ET).timestamp()

    txn_slug = cache._slug("-".join(sorted(["mTransactions2", "mTeam"])))
    other_slug = cache._slug("-".join(sorted(["mMatchupScore", "mTeam"])))
    (season_dir / f"{txn_slug}-aaaa.meta.json").write_text(json.dumps({"fetched_at": old}))
    (season_dir / f"{other_slug}-bbbb.meta.json").write_text(json.dumps({"fetched_at": new}))

    monkeypatch.setattr(loaders.config, "RAW_DIR", tmp_path)

    # The feed-level view reports the newest ANY view landed -- that is its
    # job, and it is exactly why it must not be used as a correctness gate.
    assert loaders._espn_freshness(season=2026)[0] == pytest.approx(new)
    # The per-view question returns the transactions payload's own age.
    assert loaders.espn_view_freshness(["mTransactions2", "mTeam"], season=2026) == pytest.approx(old)


def test_espn_view_freshness_is_none_when_that_view_never_ran(tmp_path, monkeypatch):
    (tmp_path / "2026").mkdir()
    monkeypatch.setattr(loaders.config, "RAW_DIR", tmp_path)
    assert loaders.espn_view_freshness(["mTransactions2", "mTeam"], season=2026) is None


def test_espn_view_freshness_is_order_independent(tmp_path, monkeypatch):
    """cache_path sorts views before slugging, so a caller reordering its
    view list must still resolve the same files."""
    from espn_ff import cache

    season_dir = tmp_path / "2026"
    season_dir.mkdir()
    ts = datetime(2026, 9, 16, 9, 8, tzinfo=ET).timestamp()
    slug = cache._slug("-".join(sorted(["mTransactions2", "mTeam"])))
    (season_dir / f"{slug}-aaaa.meta.json").write_text(json.dumps({"fetched_at": ts}))
    monkeypatch.setattr(loaders.config, "RAW_DIR", tmp_path)

    a = loaders.espn_view_freshness(["mTransactions2", "mTeam"], season=2026)
    b = loaders.espn_view_freshness(["mTeam", "mTransactions2"], season=2026)
    assert a == b == pytest.approx(ts)


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
