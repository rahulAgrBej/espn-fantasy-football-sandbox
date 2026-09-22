"""Tuesday report assembly logic -- the closure gate, per-slot regret, the
optimal-lineup DP, slot-eligibility fallback, and standings. Fixtures
only, no network, no disk."""

from datetime import datetime

import pandas as pd
import pytest

from espn_ff.report import monday, tuesday
from espn_ff.weeks import ET

import payload_helpers

_FIXED_RENDERED_AT = 1789504260  # Tue 2026-09-15 16:31 ET


def _game(week, matchup_id, away_id, away_name, home_id, home_name, winner, away_final, home_final):
    return [
        {
            "week": week, "matchup_id": matchup_id, "side": "away", "team_id": away_id, "team_name": away_name,
            "points_final": away_final, "opponent_id": home_id, "opponent_name": home_name,
            "winner": winner, "result": float("nan"),
        },
        {
            "week": week, "matchup_id": matchup_id, "side": "home", "team_id": home_id, "team_name": home_name,
            "points_final": home_final, "opponent_id": away_id, "opponent_name": away_name,
            "winner": winner, "result": float("nan"),
        },
    ]


def _roster_row(player_id, player_name, position, lineup_slot, started, points, injury_status="ACTIVE", week=1, team_id=5):
    return {
        "week": week, "team_id": team_id, "player_id": player_id, "player_name": player_name,
        "position": position, "lineup_slot": lineup_slot, "started": started, "points": points,
        "injury_status": injury_status,
    }


# ---- closure gate ----------------------------------------------------------


def test_unrefreshed_week_reports_as_unclosed_not_as_a_loss():
    """The observed today-shape: every matchup UNDECIDED, points_final 0.0
    -- the 09:08 refresh has not landed. Must never render as a loss."""
    rows = _game(1, 1, 5, "Us", 6, "Them", "UNDECIDED", 0.0, 0.0) + _game(1, 2, 7, "A", 8, "B", "UNDECIDED", 0.0, 0.0)
    result = tuesday.week_closed(pd.DataFrame(rows), week=1, team_id=5)
    assert result["insufficient"] is True
    assert result["state"] == "unclosed"


def test_decided_winner_with_both_sides_at_zero_is_incoherent():
    """A decided game cannot legitimately be 0-0 -- winner set with both
    sides still at points_final 0.0 means the score half of the refresh
    did not land, not a real tie."""
    rows = _game(1, 1, 5, "Us", 6, "Them", "HOME", 0.0, 0.0)
    result = tuesday.week_closed(pd.DataFrame(rows), week=1, team_id=5)
    assert result["insufficient"] is True
    assert result["state"] == "incoherent"


def test_one_side_at_zero_is_a_real_result():
    """Guards the coherence check against being over-eager: a
    legitimately zero-scoring team is only ever one side, never both."""
    rows = _game(1, 1, 5, "Us", 6, "Them", "HOME", 0.0, 20.0)
    result = tuesday.week_closed(pd.DataFrame(rows), week=1, team_id=5)
    assert result["insufficient"] is False
    assert result["result"] == "L"
    assert result["our_points"] == 0.0


def test_result_derives_from_winner_when_the_column_is_float_nan():
    """`result` is float64 NaN until a week closes -- `row["result"] in
    ("W","L")` silently returns False on today's data, so this must
    re-derive from winner + side rather than trust the column's dtype."""
    rows = _game(1, 1, 5, "Us", 6, "Them", "AWAY", 30.0, 20.0)
    df = pd.DataFrame(rows)
    assert df["result"].dtype == float
    result = tuesday.week_closed(df, week=1, team_id=5)
    assert result["result"] == "W"


def test_result_is_never_read_from_the_points_column():
    """`points` carries live scores while `points_final` reads 0.0 -- this
    must still render unclosed, the exact error this report exists to
    prevent."""
    rows = [
        {"week": 1, "matchup_id": 1, "side": "away", "team_id": 5, "team_name": "Us",
         "points": 88.0, "points_final": 0.0, "opponent_id": 6, "opponent_name": "Them",
         "winner": "UNDECIDED", "result": float("nan")},
        {"week": 1, "matchup_id": 1, "side": "home", "team_id": 6, "team_name": "Them",
         "points": 70.0, "points_final": 0.0, "opponent_id": 5, "opponent_name": "Us",
         "winner": "UNDECIDED", "result": float("nan")},
    ]
    result = tuesday.week_closed(pd.DataFrame(rows), week=1, team_id=5)
    assert result["insufficient"] is True
    assert result["state"] == "unclosed"


def test_our_matchup_open_while_others_closed_is_partial():
    rows = _game(1, 1, 5, "Us", 6, "Them", "UNDECIDED", 0.0, 0.0) + _game(1, 2, 7, "A", 8, "B", "HOME", 50.0, 60.0)
    result = tuesday.week_closed(pd.DataFrame(rows), week=1, team_id=5)
    assert result["insufficient"] is True
    assert result["state"] == "partial"


# ---- regret and optimal lineup ---------------------------------------------


def test_per_slot_regret_ranks_by_gap_descending():
    rosters_df = pd.DataFrame(
        [
            _roster_row("rb_starter", "RB Starter", "RB", "RB", True, 10.0),
            _roster_row("wr_starter", "WR Starter", "WR", "WR", True, 20.0),
            _roster_row("rb_bench", "RB Bench", "RB", "Bench", False, 25.0),
            _roster_row("wr_bench", "WR Bench", "WR", "Bench", False, 22.0),
        ]
    )
    pool_df = pd.DataFrame(
        [
            {"player_id": "rb_bench", "eligible_slots": "RB,RB/WR,Bench,IR"},
            {"player_id": "wr_bench", "eligible_slots": "RB/WR,WR,Bench,IR"},
        ]
    )
    allowed = {"RB", "RB/WR", "WR", "Bench", "IR"}
    rows, _, _ = tuesday.regret_table(rosters_df, week=1, team_id=5, pool_df=pool_df, allowed_slots=allowed)
    assert [r["slot"] for r in rows] == ["RB", "WR"]
    assert rows[0]["gap"] == pytest.approx(15.0)
    assert rows[1]["gap"] == pytest.approx(2.0)


def test_the_same_bench_player_may_fix_two_slots():
    """Pins the intended non-additive behavior -- Hubbard is correctly the
    best answer for both the RB and RB/WR rows, independently."""
    rosters_df = pd.DataFrame(
        [
            _roster_row("rb_starter", "Skattebo", "RB", "RB", True, 14.1),
            _roster_row("rbwr_starter", "Stevenson", "RB", "RB/WR", True, 12.0),
            _roster_row("hubbard", "Hubbard", "RB", "Bench", False, 22.2),
        ]
    )
    pool_df = pd.DataFrame([{"player_id": "hubbard", "eligible_slots": "RB,RB/WR,Bench,IR"}])
    allowed = {"RB", "RB/WR", "Bench", "IR"}
    rows, _, _ = tuesday.regret_table(rosters_df, week=1, team_id=5, pool_df=pool_df, allowed_slots=allowed)
    assert {r["slot"] for r in rows} == {"RB", "RB/WR"}
    assert all(r["bench_name"] == "Hubbard" for r in rows)


def test_ir_slot_players_are_never_regret_candidates():
    rosters_df = pd.DataFrame(
        [
            _roster_row("rb_starter", "RB Starter", "RB", "RB", True, 10.0),
            _roster_row("ir_player", "IR Guy", "RB", "IR", False, 100.0),
        ]
    )
    pool_df = pd.DataFrame([{"player_id": "ir_player", "eligible_slots": "RB,Bench,IR"}])
    allowed = {"RB", "Bench", "IR"}
    rows, _, _ = tuesday.regret_table(rosters_df, week=1, team_id=5, pool_df=pool_df, allowed_slots=allowed)
    assert rows == []


def test_a_zero_bench_player_outscores_a_negative_starter():
    """Real week-1 case: a bench player at 0.0 genuinely outscores a
    starter who went negative (Texans D/ST, -1.00)."""
    rosters_df = pd.DataFrame(
        [
            _roster_row("dst_starter", "Texans D/ST", "D/ST", "D/ST", True, -1.0),
            _roster_row("dst_bench", "Bench D/ST", "D/ST", "Bench", False, 0.0),
        ]
    )
    pool_df = pd.DataFrame([{"player_id": "dst_bench", "eligible_slots": "D/ST,Bench,IR"}])
    allowed = {"D/ST", "Bench", "IR"}
    rows, _, _ = tuesday.regret_table(rosters_df, week=1, team_id=5, pool_df=pool_df, allowed_slots=allowed)
    assert len(rows) == 1
    assert rows[0]["gap"] == pytest.approx(1.0)


def test_nan_starter_is_listed_as_unscored_not_silently_optimal():
    """Every comparison against NaN is False -- an unhandled NaN starter
    would silently render as if no bench player could beat them."""
    rosters_df = pd.DataFrame(
        [
            _roster_row("qb_starter", "NaN QB", "QB", "QB", True, float("nan")),
            _roster_row("qb_bench", "Bench QB", "QB", "Bench", False, 15.0),
        ]
    )
    pool_df = pd.DataFrame([{"player_id": "qb_bench", "eligible_slots": "QB,Bench,IR"}])
    allowed = {"QB", "Bench", "IR"}
    rows, _, nan_names = tuesday.regret_table(rosters_df, week=1, team_id=5, pool_df=pool_df, allowed_slots=allowed)
    assert rows == []
    assert "NaN QB" in nan_names


# Real week-1 export for team 5, transcribed from data/out/15-09-2026-*.csv --
# used as a closed-form regression pin for the DP, not just a property test.
_REAL_WEEK1_ROSTER = [
    ("Caleb Williams", "QB", "QB", True, 37.26),
    ("Kenneth Walker III", "RB", "RB", True, 32.60),
    ("Cam Skattebo", "RB", "RB", True, 14.10),
    ("Rhamondre Stevenson", "RB", "RB/WR", True, 12.00),
    ("CeeDee Lamb", "WR", "WR", True, 12.90),
    ("Malik Nabers", "WR", "WR", True, 9.90),
    ("Mark Andrews", "TE", "TE", True, 6.90),
    ("Seahawks D/ST", "D/ST", "D/ST", True, 12.00),
    ("Tyler Loop", "K", "K", True, 14.00),
    ("DK Metcalf", "WR", "Bench", False, 6.00),
    ("Chuba Hubbard", "RB", "Bench", False, 22.20),
    ("RJ Harvey", "RB", "Bench", False, 6.10),
    ("De'Zhaun Stribling", "WR", "Bench", False, 0.00),
    ("Tre Tucker", "WR", "Bench", False, 3.70),
    ("Jordan Love", "QB", "Bench", False, 20.48),
]

_ELIGIBLE_BY_POSITION = {
    "QB": "QB,OP,Bench,IR",
    "RB": "RB,RB/WR,FLEX,OP,Bench,IR",
    "WR": "RB/WR,WR,WR/TE,FLEX,OP,Bench,IR",
    "TE": "WR/TE,TE,FLEX,OP,Bench,IR",
    "K": "K,Bench,IR",
    "D/ST": "D/ST,Bench,IR",
}

_REAL_ROSTER_SLOTS = pd.DataFrame(
    [
        {"slot": "QB", "count": 1}, {"slot": "RB", "count": 2}, {"slot": "RB/WR", "count": 1},
        {"slot": "WR", "count": 2}, {"slot": "TE", "count": 1}, {"slot": "D/ST", "count": 1},
        {"slot": "K", "count": 1}, {"slot": "Bench", "count": 6}, {"slot": "IR", "count": 3},
    ]
)


def _real_week1_fixtures():
    rosters_df = pd.DataFrame(
        [_roster_row(name, name, position, lineup_slot, started, points)
         for name, position, lineup_slot, started, points in _REAL_WEEK1_ROSTER]
    )
    pool_df = pd.DataFrame(
        [{"player_id": name, "player_name": name, "position": position, "eligible_slots": _ELIGIBLE_BY_POSITION[position]}
         for name, position, *_rest in _REAL_WEEK1_ROSTER]
    )
    return rosters_df, pool_df


def test_optimal_points_never_below_actual_and_never_above_the_gap_sum():
    """Both formal invariants in one property test, against the real
    week-1 roster this plan hand-verified: optimal >= actual, and
    left_on_table <= sum(gap) -- the second is why the regret table must
    not be summed (18.3 of per-slot gap versus a true 10.2)."""
    rosters_df, pool_df = _real_week1_fixtures()
    allowed = monday.league_slots(_REAL_ROSTER_SLOTS)
    regret_rows, _, _ = tuesday.regret_table(rosters_df, week=1, team_id=5, pool_df=pool_df, allowed_slots=allowed)
    optimal = tuesday.optimal_lineup(
        rosters_df, week=1, team_id=5, pool_df=pool_df, roster_slots_df=_REAL_ROSTER_SLOTS, allowed_slots=allowed
    )

    gap_sum = sum(r["gap"] for r in regret_rows)
    assert optimal["optimal_points"] >= optimal["actual_points"]
    assert optimal["left_on_table"] <= gap_sum

    assert optimal["actual_points"] == pytest.approx(151.66)
    assert optimal["optimal_points"] == pytest.approx(161.86)
    assert optimal["left_on_table"] == pytest.approx(10.2)
    assert gap_sum == pytest.approx(18.3)


def test_dp_agrees_with_the_closed_form_oracle():
    """Top-2 RB, top-2 WR, RB/WR = best remaining RB-or-WR. If the DP and
    this exchange-argument oracle disagree, one of them is wrong."""
    roster_slots = pd.DataFrame(
        [{"slot": "RB", "count": 2}, {"slot": "RB/WR", "count": 1}, {"slot": "WR", "count": 2}]
    )
    allowed = monday.league_slots(roster_slots)
    rb_points = {"RB1": 20.0, "RB2": 18.0, "RB3": 15.0, "RB4": 10.0}
    wr_points = {"WR1": 25.0, "WR2": 22.0, "WR3": 19.0, "WR4": 5.0}

    rows, pool_rows = [], []
    for name, pts in rb_points.items():
        rows.append(_roster_row(name, name, "RB", "Bench", False, pts))
        pool_rows.append({"player_id": name, "eligible_slots": "RB,RB/WR,Bench,IR"})
    for name, pts in wr_points.items():
        rows.append(_roster_row(name, name, "WR", "Bench", False, pts))
        pool_rows.append({"player_id": name, "eligible_slots": "RB/WR,WR,Bench,IR"})

    result = tuesday.optimal_lineup(
        pd.DataFrame(rows), week=1, team_id=5, pool_df=pd.DataFrame(pool_rows),
        roster_slots_df=roster_slots, allowed_slots=allowed,
    )
    assert result["optimal_points"] == pytest.approx(104.0)
    assert {row["player_name"] for row in result["lineup"]} == {"RB1", "RB2", "WR1", "WR2", "WR3"}


def test_solve_slots_no_eligible_candidate_yields_negative_one_not_a_crash():
    """A slot with zero eligible candidates must surface as -1, not raise --
    the extracted DP core's own contract, independent of any caller's rows."""
    values = [10.0, 8.0]
    eligibility = [{"QB"}, {"QB"}]
    slot_list = ["QB", "RB"]
    total, ordered_slots, chosen = tuesday.solve_slots(values, eligibility, slot_list)
    assert total == pytest.approx(10.0)
    rb_index = ordered_slots.index("RB")
    assert chosen[rb_index] == -1


def test_rosters_are_never_read_from_the_wrong_week():
    """A missing week filter would silently mix a post-waiver roster into
    a retrospective -- this frame carries both weeks and only week 1
    should be reviewed."""
    rosters_df = pd.DataFrame(
        [
            _roster_row("rb_starter", "RB Starter", "RB", "RB", True, 10.0, week=1),
            _roster_row("rb_bench", "RB Bench", "RB", "Bench", False, 5.0, week=1),
            _roster_row("rb_starter", "RB Starter", "RB", "RB", True, 10.0, week=2),
            _roster_row("rb_bench", "RB Bench", "RB", "Bench", False, 50.0, week=2),
        ]
    )
    pool_df = pd.DataFrame([{"player_id": "rb_bench", "eligible_slots": "RB,Bench,IR"}])
    allowed = {"RB", "Bench", "IR"}
    rows, _, _ = tuesday.regret_table(rosters_df, week=1, team_id=5, pool_df=pool_df, allowed_slots=allowed)
    assert rows == []


# ---- eligibility, standings, edges -----------------------------------------


def test_position_fallback_when_the_pool_has_no_row():
    pool_df = pd.DataFrame(columns=["player_id", "player_name", "eligible_slots"])
    allowed = {"RB", "RB/WR", "WR", "Bench", "IR"}
    slots, used_fallback = tuesday._eligible_slots("p1", "RB", pool_df, allowed)
    assert used_fallback is True
    assert slots == {"RB", "RB/WR"}


def test_te_is_not_eligible_for_the_rb_wr_slot():
    """TE's raw eligibility carries WR/TE and FLEX so it *looks*
    flex-capable, but this league rosters neither and TE is not RB/WR
    eligible while WR is."""
    pool_df = pd.DataFrame(columns=["player_id", "eligible_slots"])
    allowed = {"TE", "RB/WR", "WR", "Bench", "IR"}
    slots, _ = tuesday._eligible_slots("p1", "TE", pool_df, allowed)
    assert slots == {"TE"}
    assert "RB/WR" not in slots


def test_slot_map_divergence_is_empty_against_the_pool():
    allowed = {"QB", "RB", "RB/WR", "WR", "TE", "K", "D/ST", "Bench", "IR"}
    pool_df = pd.DataFrame(
        [
            {"player_id": 1, "player_name": "QB1", "position": "QB", "eligible_slots": "QB,OP,Bench,IR"},
            {"player_id": 2, "player_name": "RB1", "position": "RB", "eligible_slots": "RB,RB/WR,FLEX,OP,Bench,IR"},
            {"player_id": 3, "player_name": "WR1", "position": "WR", "eligible_slots": "RB/WR,WR,WR/TE,FLEX,OP,Bench,IR"},
            {"player_id": 4, "player_name": "TE1", "position": "TE", "eligible_slots": "WR/TE,TE,FLEX,OP,Bench,IR"},
            {"player_id": 5, "player_name": "K1", "position": "K", "eligible_slots": "K,Bench,IR"},
            {"player_id": 6, "player_name": "D1", "position": "D/ST", "eligible_slots": "D/ST,Bench,IR"},
        ]
    )
    assert tuesday.slot_map_divergence(pool_df, allowed).empty


def test_pre_season_standings_snapshot_renders_insufficient_data():
    teams_df = pd.DataFrame(
        [
            {"team_id": i, "team_name": f"T{i}", "wins": 0, "losses": 0, "ties": 0, "points_for": 0.0, "playoff_seed": i}
            for i in range(1, 13)
        ]
    )
    result = tuesday.standings(teams_df, week=1)
    assert result["insufficient"] is True


def test_sole_backup_at_a_one_deep_position_is_never_a_drop_candidate():
    """Dropping the only backup QB trades a real bye-week problem for a
    marginal add -- must never be offered as a drop candidate."""
    rosters_df = pd.DataFrame(
        [
            _roster_row("qb1", "Starter QB", "QB", "QB", True, 20.0),
            _roster_row("qb2", "Backup QB", "QB", "Bench", False, 5.0),
            _roster_row("wr1", "Starter WR", "WR", "WR", True, 15.0),
            _roster_row("wr2", "Bench WR A", "WR", "Bench", False, 3.0),
            _roster_row("wr3", "Bench WR B", "WR", "Bench", False, 2.0),
        ]
    )
    pool_df = pd.DataFrame(columns=["player_id", "season_points", "season_projected"])
    drops = tuesday.drop_candidates(rosters_df, pool_df, week=1, team_id=5)
    names = [d["player_name"] for d in drops]
    assert "Backup QB" not in names
    assert "Bench WR A" in names
    assert "Bench WR B" in names


def test_week_one_has_no_prior_week_to_review():
    result = tuesday.build(2026, week=1, team_id=5)
    assert "no prior week to review" in result.markdown.lower()
    payload_helpers.assert_payload_matches_markdown(
        result.markdown, result.data["sections"]
    )


# ---- header: covered dates ------------------------------------------------


def _empty_render_args():
    closure = {"insufficient": True, "state": "missing", "reason": "no matchups export on disk"}
    standings_result = {"insufficient": True, "reason": "no teams export on disk"}
    ir_now = pd.DataFrame(columns=["player_name", "position"])
    ir_maybe = pd.DataFrame(columns=["player_name", "position", "injury_status"])
    return closure, standings_result, [], None, [], ir_now, ir_maybe


def test_render_header_states_the_reviewed_weeks_own_window():
    closure, standings_result, regret_rows, optimal, drops, ir_now, ir_maybe = _empty_render_args()
    window = (datetime(2026, 9, 8, 3, 0, tzinfo=ET), datetime(2026, 9, 15, 3, 0, tzinfo=ET))
    text = tuesday.render(
        2026, 1, closure, standings_result, regret_rows, optimal, drops, ir_now, ir_maybe,
        list(tuesday.FOOTER_NOTES), window=window, rendered_at=_FIXED_RENDERED_AT,
    )
    assert "# Week 1 in review -- 2026" in text
    assert "**Week 1** Tue 2026-09-08 03:00 - Tue 2026-09-15 03:00 ET" in text
    assert "**Rendered** Tue 2026-09-15 16:31 ET" in text


def test_render_header_shows_insufficient_data_when_the_calendar_is_absent():
    closure, standings_result, regret_rows, optimal, drops, ir_now, ir_maybe = _empty_render_args()
    text = tuesday.render(
        2026, 1, closure, standings_result, regret_rows, optimal, drops, ir_now, ir_maybe,
        list(tuesday.FOOTER_NOTES), rendered_at=_FIXED_RENDERED_AT,
    )
    assert "**Week 1** insufficient data" in text


# ---- payload: the JSON twin ---------------------------------------------

def _populated_render_args():
    """Every section populated: a closed week, standings, regret rows, an
    optimal-lineup headline, drops, and both IR lists."""
    closure = {
        "insufficient": False, "state": "final", "result": "L",
        "our_points": 98.4, "their_points": 104.2, "margin": -5.8,
        "opponent_name": "Them", "partial_note": None,
    }
    standings_result = {
        "insufficient": False,
        "rows": pd.DataFrame([
            {"playoff_seed": 1, "team_name": "Them", "wins": 2, "losses": 0, "ties": 0,
             "points_for": 210.5},
            {"playoff_seed": 2, "team_name": "Us", "wins": 1, "losses": 1, "ties": 0,
             "points_for": 198.1},
        ]),
        "games_note": "one game still in progress",
    }
    regret_rows = [{
        "slot": "RB", "starter_name": "Started Guy", "starter_points": 4.2,
        "bench_name": "Bench Guy", "bench_points": 18.9, "gap": 14.7,
    }]
    optimal = {"left_on_table": 14.7, "optimal_points": 113.1, "actual_points": 98.4}
    drops = [{"player_name": "Deadweight", "position": "WR", "ros_projection": 2.1}]
    ir_now = pd.DataFrame([{"player_name": "Hurt Guy", "position": "RB"}])
    ir_maybe = pd.DataFrame([
        {"player_name": "Maybe Guy", "position": "TE", "injury_status": "OUT"}
    ])
    return (2026, 1, closure, standings_result, regret_rows, optimal, drops,
            ir_now, ir_maybe, list(tuesday.FOOTER_NOTES))


def _empty_args():
    closure, standings_result, regret_rows, optimal, drops, ir_now, ir_maybe = _empty_render_args()
    return (2026, 1, closure, standings_result, regret_rows, optimal, drops,
            ir_now, ir_maybe, list(tuesday.FOOTER_NOTES))


@pytest.mark.parametrize("args_name", ["empty", "populated"])
def test_payload_names_the_same_sections_as_the_markdown(monkeypatch, args_name):
    monkeypatch.setattr(tuesday, "freshness", lambda season=None: {
        "sleeper": (None, True), "nflverse": (None, True),
        "espn": (None, True), "odds": (None, True),
    })
    args = _empty_args() if args_name == "empty" else _populated_render_args()
    kwargs = {"rendered_at": _FIXED_RENDERED_AT}

    text = tuesday.render(*args, **kwargs)
    header, sections = tuesday.payload(*args, **kwargs)

    payload_helpers.assert_payload_matches_markdown(text, sections)
    payload_helpers.assert_no_display_strings(sections)
    payload_helpers.assert_json_serializable(header, sections)


def test_payload_keeps_the_regret_headline_out_of_the_prose(monkeypatch):
    """"points left on the table" is the one number in this report that adds
    up, and in markdown it exists only inside a bolded sentence."""
    monkeypatch.setattr(tuesday, "freshness", lambda season=None: {"sleeper": (None, True)})
    _, sections = tuesday.payload(*_populated_render_args(), rendered_at=_FIXED_RENDERED_AT)
    regret = next(s for s in sections if s["id"] == "optimal-lineup-regret")
    assert regret["data"]["left_on_table"] == 14.7
    assert regret["data"]["optimal_points"] == 113.1


def test_payload_suppressed_regret_headline_is_insufficient_not_zero(monkeypatch):
    """optimal=None means a NaN score made the figure uncomputable. Emitting
    0 there would read as "you played the perfect lineup"."""
    monkeypatch.setattr(tuesday, "freshness", lambda season=None: {"sleeper": (None, True)})
    args = list(_populated_render_args())
    args[5] = None
    _, sections = tuesday.payload(*args, rendered_at=_FIXED_RENDERED_AT)
    regret = next(s for s in sections if s["id"] == "optimal-lineup-regret")
    assert regret["data"]["left_on_table"] is None
    assert regret["blocks"][0]["kind"] == "insufficient"


def test_payload_standings_carries_sortable_record_parts(monkeypatch):
    monkeypatch.setattr(tuesday, "freshness", lambda season=None: {"sleeper": (None, True)})
    _, sections = tuesday.payload(*_populated_render_args(), rendered_at=_FIXED_RENDERED_AT)
    standings = next(s for s in sections if s["id"] == "standings")
    assert standings["rows"][0]["record"] == "2-0-0"
    assert standings["rows"][0]["wins"] == 2


# ---- build: the reviewed-week path ---------------------------------------


def test_build_renders_the_reviewed_week_path(monkeypatch):
    """The only other build() test takes `review_week < 1`'s early return, so
    nothing exercised the path every real Tuesday run takes -- which is how a
    `rendered_at` referenced before assignment shipped and failed both Tuesday
    reports on 2026-09-22. Assert the pinned timestamp reaches the roster gate
    as one clock read, shared with the emitters."""
    rosters_df, pool_df = _real_week1_fixtures()
    # _real_week1_fixtures() carries only what regret/optimal read; drop
    # candidates additionally need the season columns.
    pool_df = pool_df.assign(season_points=0.0, season_projected=0.0)
    teams_df = pd.DataFrame(
        [
            {"team_id": i, "team_name": f"T{i}", "wins": 1, "losses": 0, "ties": 0,
             "points_for": 100.0 + i, "playoff_seed": i}
            for i in range(1, 13)
        ]
    )
    matchups_df = pd.DataFrame(_game(1, 1, 5, "T5", 6, "T6", "HOME", 120.0, 130.0))
    exports = {
        "matchups": matchups_df, "teams": teams_df, "weekly-rosters": rosters_df,
        "player-pool": pool_df, "roster-slots": _REAL_ROSTER_SLOTS,
    }
    monkeypatch.setattr(tuesday, "latest_export", lambda name: exports[name])
    monkeypatch.setattr(tuesday, "freshness", lambda season=None: {"espn": (None, False)})

    seen = []
    monkeypatch.setattr(
        tuesday, "roster_staleness_note",
        lambda rendered_at, **kwargs: seen.append(rendered_at) or None,
    )

    result = tuesday.build(2026, week=2, team_id=5)

    assert seen and isinstance(seen[0], float)
    assert result.data["header"]["rendered_at"] == seen[0]
    payload_helpers.assert_payload_matches_markdown(
        result.markdown, result.data["sections"]
    )
