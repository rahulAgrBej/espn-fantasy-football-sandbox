"""The stat selector is the correctness core -- these are its guardrails.

A player's stats[] mixes seasons. Whether a naive (statSourceId, scoringPeriodId)
filter returns the right row depends on arbitrary array ordering: across a
300-player sample of the live 2026 pool, 128 of the 280 players with week-1 data
resolved to the *2025* row. These tests pin both orderings.
"""

import pytest

from espn_ff import stats


def _naive(player, week):
    """The espn_team.py logic: first row matching source and period only."""
    return next(
        (
            s
            for s in player["stats"]
            if s.get("statSourceId") == 0 and s.get("scoringPeriodId") == week
        ),
        None,
    )


def test_fixture_still_mixes_seasons(order_unlucky):
    """Guards the premise. If this fails, the fixture proves nothing."""
    weekly = [
        s
        for s in order_unlucky["stats"]
        if s["statSourceId"] == stats.ACTUAL and s["statSplitTypeId"] == stats.SPLIT_GAME
    ]
    assert len({s["seasonId"] for s in weekly}) > 1
    assert len([s for s in weekly if s["scoringPeriodId"] == 1]) > 1


def test_naive_filter_returns_the_wrong_season(order_unlucky):
    """The regression this package exists to prevent."""
    assert _naive(order_unlucky, 1)["seasonId"] == 2025
    assert stats.weekly_points(order_unlucky, season=2026, week=1) == pytest.approx(35.66)
    assert stats.weekly_points(order_unlucky, season=2025, week=1) == pytest.approx(38.76)


def test_selector_is_immune_to_array_order(order_lucky, order_unlucky):
    """Same call, opposite orderings, both correct."""
    assert _naive(order_lucky, 1)["seasonId"] == 2026
    assert _naive(order_unlucky, 1)["seasonId"] == 2025
    for player in (order_lucky, order_unlucky):
        row = stats.weekly_stat(player, season=2026, week=1)
        assert row["seasonId"] == 2026
        assert row["statSplitTypeId"] == stats.SPLIT_GAME


def test_every_fixture_player_resolves_to_the_asked_season(player_pool):
    for entry in player_pool["players"]:
        for season in (2025, 2026):
            row = stats.weekly_stat(entry["player"], season=season, week=1)
            if row is not None:
                assert row["seasonId"] == season


def test_projection_is_a_separate_source(order_lucky):
    actual = stats.weekly_points(order_lucky, season=2026, week=1)
    projected = stats.weekly_points(order_lucky, season=2026, week=1, projected=True)
    assert projected > 0 and projected != pytest.approx(actual)


def test_season_total_uses_the_season_split(order_lucky):
    """Season rows carry scoringPeriodId 0 and splitType 0."""
    row = stats.season_stat(order_lucky, season=2025)
    assert row["scoringPeriodId"] == 0
    assert row["statSplitTypeId"] == stats.SPLIT_SEASON
    assert stats.season_points(order_lucky, season=2025) == pytest.approx(366.9)


def test_unplayed_week_is_zero_not_an_error(order_lucky):
    assert stats.weekly_points(order_lucky, season=2026, week=17) == 0.0
    assert stats.weekly_stat(order_lucky, season=1999, week=1) is None


def test_applied_total_tolerates_junk():
    assert stats.applied_total(None) == 0.0
    assert stats.applied_total({}) == 0.0
    assert stats.applied_total({"appliedTotal": None}) == 0.0
    assert stats.applied_total({"appliedTotal": 12}) == 12.0


def test_player_with_no_stats_key():
    assert stats.weekly_points({}, season=2026, week=1) == 0.0
