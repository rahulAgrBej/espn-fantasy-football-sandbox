"""The free-agent anti-join: `docs/report-weekly-schedule.md`'s standing
known gap, closed here.

`player-pool.csv` carries `on_team_id = 0` on every row -- it comes from
the ownership-free `leaguedefaults/3` pool (`config.PLAYER_POOL_ID`), so
that column cannot distinguish a free agent from a rostered player and
**must never be read directly** (espn_ff/extract/players.py is its only
producer and nothing consumes it). Availability is derived instead:

    free_agents(week) = player-pool.csv (week == N)
                        ANTI JOIN weekly-rosters.csv (all teams, week == N)
                        ON player_id

"Free agent" here means "unowned *and* in ESPN's default ~1,041-player
pool", not "every unowned NFL player" -- a player outside that pool never
appears as a free agent even though no team rosters them either.
"""

from .loaders import latest_export


def free_agents(week, pool_df=None, rosters_df=None):
    """Unowned players for `week`, per the anti-join above. `pool_df`/
    `rosters_df` default to the latest data/out/ exports; pass them
    explicitly to avoid re-reading disk when a caller already has both."""
    pool_df = latest_export("player-pool") if pool_df is None else pool_df
    rosters_df = latest_export("weekly-rosters") if rosters_df is None else rosters_df
    if pool_df.empty:
        return pool_df

    week_pool = pool_df[pool_df["week"] == week]
    rostered_ids = (
        set(rosters_df.loc[rosters_df["week"] == week, "player_id"])
        if not rosters_df.empty
        else set()
    )
    return week_pool[~week_pool["player_id"].isin(rostered_ids)].reset_index(drop=True)
