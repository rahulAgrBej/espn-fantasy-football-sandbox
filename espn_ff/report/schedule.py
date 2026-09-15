"""Kickoff-day slicing of nflverse's schedule, for reports that need to
know which games are still to be played in a given fantasy week.

nflverse's `home_team`/`away_team` use its own abbreviations (`LA`, `WAS`,
`JAC`), while ESPN's `pro_team` (weekly-rosters.csv) uses `LAR`, `WSH`,
`JAX`. Every team code returned from here is passed through
espn_ff/names.py's normalize_team so the two vocabularies line up -- reuse
the one alias table rather than re-deriving it.
"""

from ..names import normalize_team
from ..nflverse import store as nflverse_store


def remaining_games(season, week, weekday="Monday"):
    """The `weekday` slice of nflverse's schedules for (season, week).

    Returns an empty frame when the week has no game on that weekday --
    callers must not assume exactly one. 2026 happens to have exactly one
    Monday game every regular-season week, but that is a property of this
    season's schedule, not a rule this function may rely on.
    """
    games = nflverse_store.load("schedules")
    subset = games[
        (games["season"] == season)
        & (games["week"] == week)
        & (games["game_type"] == "REG")
        & (games["weekday"] == weekday)
    ].copy()
    if subset.empty:
        return subset
    subset["home_team"] = subset["home_team"].map(normalize_team)
    subset["away_team"] = subset["away_team"].map(normalize_team)
    return subset.reset_index(drop=True)


def teams_in(games_df):
    """Every normalized team code appearing in `games_df`'s home/away
    columns, as a set -- the filter every downstream section applies to
    decide whether a player's pro_team is "in tonight's game"."""
    if games_df.empty:
        return set()
    return set(games_df["home_team"]) | set(games_df["away_team"])
