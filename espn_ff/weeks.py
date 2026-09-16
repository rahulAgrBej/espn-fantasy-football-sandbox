"""The fantasy week as a calendar window, sourced from ESPN's own published
season calendar (`chui_default_platformsettings`'s `scoringPeriods`) --
nothing else in this repo maps a week number back to the dates it covers.

The boundary is the league's own convention -- Tue 03:00 ET to Tue 03:00
ET, per ESPN's calendar -- pinned to ET **wall-clock**, not a fixed UTC
offset: period 8 -> 9 crosses the Nov 1 DST fallback and both endpoints
still land on Tue 03:00 ET, so this module converts through `ZoneInfo`
rather than assuming a constant millisecond stride (first use of
`ZoneInfo` in `espn_ff/`).

No network, no client instance: reads whatever calendar is already cached
on disk via `client.platform_settings_cache_path`, the same file
`EspnClient.current_scoring_period` resolves against, however old it is.
Returns `None` rather than fetching or guessing when that calendar isn't
there -- callers render the words, never a guessed range.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import cache
from .client import platform_settings_cache_path

ET = ZoneInfo("America/New_York")


def _to_et(epoch_ms):
    return datetime.fromtimestamp(epoch_ms / 1000, tz=ET)


def period_windows(scoring_periods):
    """{id: (start_et, end_et)} for every real scoring period. Skips the
    id-0 preseason sentinel, exactly as client.resolve_scoring_period
    does."""
    windows = {}
    for period in scoring_periods or []:
        period_id = period.get("id")
        start, end = period.get("startDate"), period.get("endDate")
        if period_id == 0 or start is None or end is None:
            continue
        windows[period_id] = (_to_et(start), _to_et(end))
    return windows


def _cached_scoring_periods(season):
    data = cache.read(platform_settings_cache_path(season))
    if data is None:
        return None
    if isinstance(data, list):
        data = data[0] if data else {}
    return data.get("scoringPeriods") or []


def week_window(season, week, scoring_periods=None):
    """(start_et, end_et) for `week`, defaulting to the calendar already
    cached on disk for `season`. Week 1 is clamped to one week before week
    2's start -- ESPN's real period 1 is a catch-all offseason window
    (Wed 03/25 -> Tue 09/15 in 2026), not this league's actual week 1.
    Returns None when no calendar is available, or `week` isn't in it
    (e.g. period 18 ends on a non-Tuesday and is reported as ESPN's own
    endpoint, but a week past the season's last period has nothing to
    report)."""
    if scoring_periods is None:
        scoring_periods = _cached_scoring_periods(season)
    if not scoring_periods:
        return None

    windows = period_windows(scoring_periods)
    if week == 1:
        if 2 not in windows:
            return None
        start_of_2, _ = windows[2]
        return (start_of_2 - timedelta(days=7), start_of_2)
    return windows.get(week)


def format_window(start, end):
    """"Tue 2026-09-15 03:00 - Tue 2026-09-22 03:00 ET"."""
    fmt = "%a %Y-%m-%d %H:%M"
    return f"{start.strftime(fmt)} - {end.strftime(fmt)} ET"
