"""Selecting the right row out of a player's stats[] array.

A player's stats[] mixes seasons, sources and split types in one flat list.
Filtering on (statSourceId, scoringPeriodId) alone -- as most community
snippets do -- silently returns the *previous* season's points for the same
week number. Every selector here keys on all four fields.
"""

# statSourceId
ACTUAL = 0
PROJECTED = 1

# statSplitTypeId
SPLIT_SEASON = 0
SPLIT_GAME = 1
SPLIT_REST_OF_SEASON = 2


def _select(player, *, season, scoring_period, source, split_type):
    for row in player.get("stats") or ():
        if (
            row.get("seasonId") == season
            and row.get("scoringPeriodId") == scoring_period
            and row.get("statSourceId") == source
            and row.get("statSplitTypeId") == split_type
        ):
            return row
    return None


def weekly_stat(player, *, season, week, projected=False):
    """The per-game stat row for one week, or None."""
    return _select(
        player,
        season=season,
        scoring_period=week,
        source=PROJECTED if projected else ACTUAL,
        split_type=SPLIT_GAME,
    )


def season_stat(player, *, season, projected=False):
    """The season-aggregate stat row, or None."""
    return _select(
        player,
        season=season,
        scoring_period=0,
        source=PROJECTED if projected else ACTUAL,
        split_type=SPLIT_SEASON,
    )


def applied_total(stat_row, default=0.0):
    """Fantasy points off a stat row, tolerating None and missing keys."""
    if not stat_row:
        return default
    total = stat_row.get("appliedTotal")
    return default if total is None else float(total)


def weekly_points(player, *, season, week, projected=False):
    return applied_total(weekly_stat(player, season=season, week=week, projected=projected))


def season_points(player, *, season, projected=False):
    return applied_total(season_stat(player, season=season, projected=projected))


def decode_stats(stat_row, dictionary):
    """Raw {stat_id: value} -> {abbrev: value} using the stat dictionary."""
    if not stat_row:
        return {}
    raw = stat_row.get("stats") or {}
    return {dictionary.get(int(k), str(k)): v for k, v in raw.items()}
