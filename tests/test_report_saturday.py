"""Saturday's contingency-check report -- the baseline-snapshot gate, the
tier diff against Friday's snapshot, the bye-week and parked-in-IR checks,
and the render/build wiring. Fixtures only, no network, no disk."""

from datetime import date

import pandas as pd

from espn_ff.report import saturday


# ---- fixture builders ------------------------------------------------------

def _roster_row(player_id, player_name, position, lineup_slot, started,
                 week=3, team_id=5, pro_team="KC"):
    return {
        "season": 2026, "week": week, "team_id": team_id, "team_name": "Us",
        "player_id": player_id, "player_name": player_name, "position": position,
        "pro_team": pro_team, "lineup_slot_id": 0, "lineup_slot": lineup_slot,
        "started": started, "injury_status": "ACTIVE", "acquisition_type": "DRAFT",
        "points": None, "projected": None,
    }


def _slim_row(sleeper_id, injury_status=None, practice_participation=None,
              status="Active", active=True, depth_chart_order=None, depth_chart_position=None):
    return {
        "sleeper_id": sleeper_id, "espn_id": None, "gsis_id": None, "full_name": None,
        "team": "KC", "position": "RB",
        "depth_chart_position": depth_chart_position, "depth_chart_order": depth_chart_order,
        "injury_status": injury_status, "injury_body_part": None, "injury_notes": None,
        "practice_participation": practice_participation, "status": status, "active": active,
        "number": None, "years_exp": None, "fetched_at": None,
    }


_EMPTY_GATE = {
    "insufficient": True, "reason": "no Sleeper slim snapshots on disk",
    "baseline_date": None, "current_date": None, "days_used": None,
}


def _render(**overrides):
    kwargs = dict(
        season=2026, week=3, team_id=5,
        gate=dict(_EMPTY_GATE), diff_rows=[], unmatched_names=set(), status_rows=[], depth_moves=[],
        bye_starters=[], parked_rows=[], official_rows=[], footer_notes=saturday.FOOTER_NOTES,
        rendered_at=1_760_000_000,
    )
    kwargs.update(overrides)
    return saturday.render(**kwargs)


# ---- baseline_snapshot: one test per rung of the ladder --------------------

def test_baseline_snapshot_no_snapshots_on_disk():
    gate = saturday.baseline_snapshot(date(2026, 9, 19), snapshots_by_date={})
    assert gate["insufficient"] is True
    assert "no Sleeper slim snapshots on disk" in gate["reason"]


def test_baseline_snapshot_todays_run_has_not_landed_names_actual_newest_date():
    snapshots = {date(2026, 9, 18): pd.DataFrame([_slim_row("s1")])}
    gate = saturday.baseline_snapshot(date(2026, 9, 19), snapshots_by_date=snapshots)
    assert gate["insufficient"] is True
    assert "has not landed" in gate["reason"]
    assert "2026-09-18" in gate["reason"]


def test_baseline_snapshot_only_one_snapshot_exists():
    today = date(2026, 9, 19)
    snapshots = {today: pd.DataFrame([_slim_row("s1")])}
    gate = saturday.baseline_snapshot(today, snapshots_by_date=snapshots)
    assert gate["insufficient"] is True
    assert "no prior day to compare" in gate["reason"]


def test_baseline_snapshot_success_names_the_actual_gap_used():
    today = date(2026, 9, 19)
    three_days_ago = date(2026, 9, 16)
    snapshots = {
        three_days_ago: pd.DataFrame([_slim_row("s1")]),
        today: pd.DataFrame([_slim_row("s1")]),
    }
    gate = saturday.baseline_snapshot(today, snapshots_by_date=snapshots)
    assert gate["insufficient"] is False
    assert gate["baseline_date"] == three_days_ago
    assert gate["current_date"] == today
    assert gate["days_used"] == 3


# ---- tier_diff --------------------------------------------------------------

def _week_rosters(rows):
    return pd.DataFrame(rows)


def _sleeper_map(pairs):
    return pd.DataFrame([{"espn_player_id": espn_id, "sleeper_id": sleeper_id} for espn_id, sleeper_id in pairs])


def test_tier_diff_detects_a_worsening_move():
    week_rosters = _week_rosters([_roster_row(1, "Player", "RB", "RB", True)])
    baseline_df = pd.DataFrame([_slim_row("s1", injury_status=None, practice_participation=None)])
    current_df = pd.DataFrame([_slim_row("s1", injury_status="QUESTIONABLE", practice_participation="DNP")])
    sleeper_df = _sleeper_map([(1, "s1")])

    rows, unmatched = saturday.tier_diff(week_rosters, baseline_df, current_df, sleeper_df)

    assert unmatched == set()
    row = rows[0]
    assert row["tier_then"] == "CLEAR"
    assert row["tier_now"] == "HIGH_RISK"
    assert row["changed"] is True
    assert row["direction"] == "worse"


def test_tier_diff_detects_a_healing_move():
    week_rosters = _week_rosters([_roster_row(1, "Player", "RB", "RB", True)])
    baseline_df = pd.DataFrame([_slim_row("s1", injury_status="OUT")])
    current_df = pd.DataFrame([_slim_row("s1", injury_status=None)])
    sleeper_df = _sleeper_map([(1, "s1")])

    rows, _ = saturday.tier_diff(week_rosters, baseline_df, current_df, sleeper_df)

    assert rows[0]["tier_then"] == "OUT"
    assert rows[0]["tier_now"] == "CLEAR"
    assert rows[0]["direction"] == "better"


def test_tier_diff_identical_snapshots_yields_zero_changed_rows():
    week_rosters = _week_rosters([_roster_row(1, "Player", "RB", "RB", True)])
    same = pd.DataFrame([_slim_row("s1", injury_status="QUESTIONABLE", practice_participation="LP")])
    sleeper_df = _sleeper_map([(1, "s1")])

    rows, _ = saturday.tier_diff(week_rosters, same, same, sleeper_df)

    assert all(not r["changed"] for r in rows)


def test_tier_diff_no_sleeper_match_is_counted_and_named_not_read_as_unchanged():
    week_rosters = _week_rosters([
        _roster_row(1, "Matched", "RB", "RB", True),
        _roster_row(2, "Unmatched", "WR", "WR", True),
    ])
    baseline_df = pd.DataFrame([_slim_row("s1")])
    current_df = pd.DataFrame([_slim_row("s1")])
    sleeper_df = _sleeper_map([(1, "s1")])

    rows, unmatched = saturday.tier_diff(week_rosters, baseline_df, current_df, sleeper_df)

    assert "Unmatched" in unmatched
    assert "Unmatched" not in {r["player_name"] for r in rows}


# ---- bye_week_starters -------------------------------------------------------

def test_bye_week_starters_flags_a_team_with_no_game(monkeypatch):
    starters = _week_rosters([
        _roster_row(1, "Playing", "RB", "RB", True, pro_team="KC"),
        _roster_row(2, "On Bye", "WR", "WR", True, pro_team="MIA"),
    ])
    games = pd.DataFrame([
        {"season": 2026, "week": 3, "game_type": "REG", "home_team": "KC", "away_team": "DEN",
         "weekday": "Sunday", "gameday": "2026-09-27", "gametime": "13:00"},
    ])
    monkeypatch.setattr(saturday.schedule.nflverse_store, "load", lambda name: games)

    result = saturday.bye_week_starters(starters, 2026, 3)

    assert [r["player_name"] for r in result] == ["On Bye"]


# ---- parked_in_ir -------------------------------------------------------------

def test_parked_in_ir_finds_a_healthy_player_not_a_genuinely_out_one():
    week_rosters = _week_rosters([
        _roster_row(1, "Stashed Healthy", "RB", "IR", False),
        _roster_row(2, "Legitimately Out", "WR", "IR", False),
    ])
    avail_by_id = {1: "CLEAR", 2: "OUT"}

    rows = saturday.parked_in_ir(week_rosters, avail_by_id)

    assert [r["player_name"] for r in rows] == ["Stashed Healthy"]


# ---- render: freshness header carries no odds: line -------------------------

def test_render_freshness_header_has_no_odds_line(monkeypatch):
    monkeypatch.setattr(saturday, "freshness", lambda season=None: {
        "sleeper": (1_760_000_000, False), "nflverse": (1_760_000_000, False),
        "espn": (1_760_000_000, True), "odds": (1_760_000_000, False),
    })
    text = _render()
    assert "- odds:" not in text
    assert "- sleeper:" in text
    assert "- espn:" in text


# ---- render: every frame empty produces a complete document -----------------

def test_render_with_every_frame_empty_produces_complete_document():
    text = _render()
    assert "# Contingency check -- 2026 week 3" in text
    assert "## Decisions due" in text
    assert "No starter is on a bye this week." in text
    assert "No healthy player is parked in an IR slot." in text
    assert "## Tier changes since --" in text
    assert saturday.INSUFFICIENT_DATA in text
    assert "## Roster status and depth chart" in text
    assert "## Today's official designations" in text
    assert "## What this report cannot see" in text
