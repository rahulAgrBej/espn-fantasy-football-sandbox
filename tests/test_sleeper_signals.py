"""Derived Sleeper signals -- fixtures only, no network."""

import datetime as dt
import json
from pathlib import Path

import pandas as pd
import pytest

from espn_ff.sleeper import signals

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "status,practice,expected",
    [
        ("Out", None, "OUT"),
        ("IR", None, "OUT"),
        ("PUP", None, "OUT"),
        ("Suspension", None, "OUT"),
        ("Doubtful", "FP", "OUT"),
        ("Doubtful", None, "OUT"),
        ("Questionable", "DNP", "HIGH_RISK"),
        ("Questionable", "LP", "COIN_FLIP"),
        ("Questionable", "FP", "LIKELY_PLAYS"),
        ("Questionable", None, "UNKNOWN"),
        (None, "FP", "CLEAR"),
        (None, None, "CLEAR"),
    ],
)
def test_availability_tier_table(status, practice, expected):
    assert signals.availability_tier(status, practice) == expected


def test_practice_trajectory_handles_a_missing_middle_day():
    today = dt.date(2026, 9, 13)
    monday = today - dt.timedelta(days=today.weekday())
    wed = monday + dt.timedelta(days=2)
    fri = monday + dt.timedelta(days=4)

    snapshots_by_date = {
        wed: pd.DataFrame([{"sleeper_id": "1001", "practice_participation": "DNP"}]),
        fri: pd.DataFrame([{"sleeper_id": "1001", "practice_participation": "FP"}]),
    }

    trajectory = signals.practice_trajectory(snapshots_by_date, "1001", today=today)
    assert trajectory == ("DNP", None, "FP")
    assert signals.format_trajectory(trajectory) == "DNP / — / FP"


def test_practice_trajectory_all_missing_renders_as_dashes():
    today = dt.date(2026, 9, 13)
    trajectory = signals.practice_trajectory({}, "1001", today=today)
    assert trajectory == (None, None, None)
    assert signals.format_trajectory(trajectory) == "— / — / —"


def test_depth_chart_delta_uses_exact_lookback_when_available():
    today = dt.date(2026, 9, 13)
    prior_day = today - dt.timedelta(days=3)
    snapshots_by_date = {
        prior_day: pd.DataFrame(
            [{"sleeper_id": "1001", "depth_chart_order": 3, "depth_chart_position": "WR"}]
        )
    }
    current_row = {"depth_chart_order": 2, "depth_chart_position": "WR"}
    delta = signals.depth_chart_delta(current_row, snapshots_by_date, "1001", lookback_days=3, today=today)
    assert delta["days_used"] == 3
    assert delta["improved"] is True
    assert delta["promoted"] is True


def test_depth_chart_delta_falls_back_to_oldest_available_snapshot():
    today = dt.date(2026, 9, 13)
    old_day = today - dt.timedelta(days=10)  # nothing exactly 3 days prior
    snapshots_by_date = {
        old_day: pd.DataFrame(
            [{"sleeper_id": "1001", "depth_chart_order": 4, "depth_chart_position": "WR"}]
        )
    }
    current_row = {"depth_chart_order": 1, "depth_chart_position": "WR"}
    delta = signals.depth_chart_delta(current_row, snapshots_by_date, "1001", lookback_days=3, today=today)
    assert delta["days_used"] == 10
    assert delta["improved"] is True
    assert delta["promoted"] is True


def test_depth_chart_delta_returns_none_with_no_prior_snapshot():
    today = dt.date(2026, 9, 13)
    delta = signals.depth_chart_delta({"depth_chart_order": 1}, {}, "1001", today=today)
    assert delta is None


def test_depth_chart_delta_not_improved_when_order_worsens():
    today = dt.date(2026, 9, 13)
    prior_day = today - dt.timedelta(days=3)
    snapshots_by_date = {
        prior_day: pd.DataFrame(
            [{"sleeper_id": "1001", "depth_chart_order": 1, "depth_chart_position": "WR"}]
        )
    }
    current_row = {"depth_chart_order": 3, "depth_chart_position": "WR"}
    delta = signals.depth_chart_delta(current_row, snapshots_by_date, "1001", lookback_days=3, today=today)
    assert delta["improved"] is False
    assert delta["promoted"] is False


@pytest.fixture
def trending_df():
    raw = json.loads((FIXTURES / "sleeper_trending_small.json").read_text())
    rows = [{"player_id": r["player_id"], "count": r["count"], "kind": "add"} for r in raw["add"]]
    rows += [{"player_id": r["player_id"], "count": r["count"], "kind": "drop"} for r in raw["drop"]]
    return pd.DataFrame(rows)


def test_trending_flag_requires_both_top_rank_and_floor(trending_df):
    flag, count = signals.trending_flag(trending_df, "1001", kind="add", limit=2, floor=1000)
    assert flag is True and count == 45000

    # Ranked out by a tighter limit.
    flag, count = signals.trending_flag(trending_df, "1006", kind="add", limit=2, floor=0)
    assert flag is False and count == 500

    # Ranked in, but under the floor.
    flag, count = signals.trending_flag(trending_df, "1002", kind="add", limit=3, floor=20000)
    assert flag is False and count == 12000


def test_trending_flag_zero_count_when_absent(trending_df):
    flag, count = signals.trending_flag(trending_df, "9999", kind="add", limit=25, floor=0)
    assert flag is False and count == 0
