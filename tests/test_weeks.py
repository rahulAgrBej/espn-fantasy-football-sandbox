"""espn_ff/weeks.py -- the week-to-calendar-window inverse. Fixture shaped
like tests/test_cache.py's synthetic scoringPeriods list, but with the
*real* 2026 epoch values (data/raw/2026/chui-default-platformsettings-*.json)
for the periods that matter: the id-0 sentinel, week 1's offseason
catch-all, the Nov 1 DST fallback (periods 8 -> 9), and period 18's
non-Tuesday end.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from espn_ff import cache, weeks

ET = ZoneInfo("America/New_York")


def _et(*args):
    return datetime(*args, tzinfo=ET)


# Real 2026 season calendar, id -> (startDate, endDate) in epoch ms
# (Observed against data/raw/2026/chui-default-platformsettings-*.json).
_REAL_PERIODS = [
    {"id": 0, "startDate": 0, "endDate": 0},
    {"id": 1, "startDate": 1774422000000, "endDate": 1789455600000},  # Wed 03/25 -> Tue 09/15, offseason catch-all
    {"id": 2, "startDate": 1789455600000, "endDate": 1790060400000},  # Tue 09/15 -> Tue 09/22
    {"id": 8, "startDate": 1793084400000, "endDate": 1793692800000},  # Tue 10/27 -> Tue 11/03 (DST fallback inside)
    {"id": 9, "startDate": 1793692800000, "endDate": 1794297600000},  # Tue 11/03 -> Tue 11/10
    {"id": 18, "startDate": 1799136000000, "endDate": 1799654400000},  # Tue 01/05 -> Mon 01/11 (non-Tuesday end)
]


def test_period_windows_skips_the_id_zero_sentinel():
    windows = weeks.period_windows(_REAL_PERIODS)
    assert 0 not in windows


def test_period_windows_converts_to_et_wall_clock():
    windows = weeks.period_windows(_REAL_PERIODS)
    start, end = windows[2]
    assert start == _et(2026, 9, 15, 3, 0)
    assert end == _et(2026, 9, 22, 3, 0)


def test_boundary_holds_tuesday_three_am_across_the_dst_fallback():
    """Period 8 -> 9 crosses the Nov 1 DST fallback and both endpoints
    still read Tue 03:00 ET -- pinned to wall-clock, not a fixed UTC
    offset or a constant 604800000ms stride."""
    windows = weeks.period_windows(_REAL_PERIODS)
    start_8, end_8 = windows[8]
    start_9, end_9 = windows[9]
    assert end_8 == start_9 == _et(2026, 11, 3, 3, 0)
    assert end_9 == _et(2026, 11, 10, 3, 0)
    assert start_8.utcoffset() != end_8.utcoffset()  # -04:00 vs -05:00 -- DST actually moved mid-period


def test_week_one_clamps_to_one_week_before_week_two_not_the_offseason_window():
    start, end = weeks.week_window(2026, 1, scoring_periods=_REAL_PERIODS)
    assert start == _et(2026, 9, 8, 3, 0)
    assert end == _et(2026, 9, 15, 3, 0)


def test_week_two_is_the_plain_calendar_window():
    start, end = weeks.week_window(2026, 2, scoring_periods=_REAL_PERIODS)
    assert start == _et(2026, 9, 15, 3, 0)
    assert end == _et(2026, 9, 22, 3, 0)


def test_period_eighteen_end_is_reported_as_espns_actual_non_tuesday_endpoint():
    start, end = weeks.week_window(2026, 18, scoring_periods=_REAL_PERIODS)
    assert start == _et(2027, 1, 5, 3, 0)
    assert end == _et(2027, 1, 11, 3, 0)
    assert end.strftime("%a") == "Mon"


def test_week_with_no_matching_period_is_none():
    assert weeks.week_window(2026, 12, scoring_periods=_REAL_PERIODS) is None


def test_week_window_is_none_when_no_calendar_is_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(cache.config, "RAW_DIR", tmp_path)
    assert weeks.week_window(2026, 2) is None


def test_week_window_reads_the_same_cache_path_current_scoring_period_uses(tmp_path, monkeypatch):
    """No client instance, no network -- weeks.py reads whatever calendar
    is already on disk via the same platform_settings_cache_path
    EspnClient.current_scoring_period resolves against."""
    monkeypatch.setattr(cache.config, "RAW_DIR", tmp_path)
    path = weeks.platform_settings_cache_path(2026)
    cache.write(path, {"scoringPeriods": _REAL_PERIODS}, url="https://example.test")

    start, end = weeks.week_window(2026, 2)
    assert start == _et(2026, 9, 15, 3, 0)
    assert end == _et(2026, 9, 22, 3, 0)


def test_format_window_matches_the_documented_shape():
    start, end = _et(2026, 9, 15, 3, 0), _et(2026, 9, 22, 3, 0)
    assert weeks.format_window(start, end) == "Tue 2026-09-15 03:00 - Tue 2026-09-22 03:00 ET"
