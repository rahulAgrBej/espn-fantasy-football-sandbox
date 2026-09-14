"""Derived nflverse role features: offense share, target share, and their
trends, at (gsis_id, season, week) grain. Pure and offline -- the
sleeper/signals.py analogue. DuckDB reads the raw parquet straight off disk
via espn_ff.nflverse.store and joins through player_xwalk.csv.

The subtle part, in order of how easy each is to get wrong:

1. A snap row with no stats_player row is a real zero, not missing data.
   Verified live: 332/395 week-1 skill-position snap rows had a
   stats_player row; the other 63, including Calvin Ridley at 64% snap
   share, had none. COALESCE(..., 0) on the stats join is load-bearing.

2. A bye is not a 0% snap week, and an inactive-but-on-the-team week is not
   either. Both are absent from snap_counts, for different reasons, and
   both must read as `bye`/`played` flags with a null offense_pct -- never
   as offense_pct = 0. `bye` comes from a (team, week) pair missing from
   the schedule expansion; the fallback of "no row at all" is what an
   unsigned player, correctly, produces.

3. Trend window functions (snap_pct_delta_*w, snap_pct_trend) must run over
   games the player actually played, never a dense week axis -- otherwise a
   bye or inactive week silently interpolates as a 0 and flips a stable
   starter's trend around a week of no information. This falls out for
   free here because the `weekly` CTE only contains rows the player has a
   snap_counts row for; window functions computed over `weekly` therefore
   already skip byes/inactive weeks. Only the final SELECT re-attaches
   those rows (with null trend columns) to preserve the bye/inactive flags.
"""

import duckdb
import pandas as pd

from .. import config
from . import store

# Offensive skill positions snap_counts assigns -- fantasy-relevant only.
# snap_counts also carries defense/special-teams positions (C, CB, DL, LB,
# K, P, ...) that have no place in a role-feature table.
SKILL_POSITIONS = {"QB", "RB", "WR", "TE", "FB", "HB"}

FEATURE_COLUMNS = [
    "gsis_id", "season", "week", "player_name", "position", "team", "opponent",
    "espn_player_id", "played", "bye", "game_completed",
    "offense_snaps", "offense_pct",
    "targets", "target_share", "air_yards_share", "wopr", "targets_per_snap",
    "snap_pct_delta_1w", "snap_pct_delta_3w", "snap_pct_trend",
    "report_status", "practice_status", "pos_rank",
    "provisional",
]


def _read_xwalk():
    if config.NFLVERSE_XWALK.exists():
        return pd.read_csv(config.NFLVERSE_XWALK, dtype={"gsis_id": str, "pfr_id": str})
    return pd.DataFrame(columns=["gsis_id", "pfr_id", "espn_player_id", "display_name", "position", "latest_team", "status", "source"])


def build(seasons, con=None):
    con = con or duckdb.connect()
    frames = [f for f in (_build_season(season, con) for season in seasons) if f is not None and not f.empty]
    if not frames:
        return pd.DataFrame(columns=FEATURE_COLUMNS)
    return pd.concat(frames, ignore_index=True)[FEATURE_COLUMNS]


def _build_season(season, con):
    schedules = store.load("schedules")
    schedules = schedules[schedules["season"] == season]
    if schedules.empty:
        return None

    snaps = store.load("snap_counts", season=season)
    stats = store.load("stats_player", season=season)
    xwalk = _read_xwalk()
    injuries = store.load("injuries", season=season)
    depth_charts = store.load("depth_charts", season=season)

    if snaps.empty:
        return None

    con.register("schedules", schedules)
    con.register("snaps", snaps)
    con.register("stats", stats)
    con.register("xwalk", xwalk)
    con.register("injuries", injuries)
    con.register("depth_charts", depth_charts)

    skill_list = ", ".join(f"'{p}'" for p in sorted(SKILL_POSITIONS))

    query = f"""
    WITH team_weeks AS (
        SELECT season, week, home_team AS team, (home_score IS NOT NULL) AS game_completed
        FROM schedules WHERE game_type = 'REG'
        UNION ALL
        SELECT season, week, away_team AS team, (home_score IS NOT NULL) AS game_completed
        FROM schedules WHERE game_type = 'REG'
    ),
    season_weeks AS (SELECT DISTINCT week FROM team_weeks),
    season_teams AS (SELECT DISTINCT team FROM team_weeks),
    full_grid AS (
        SELECT st.team, sw.week FROM season_teams st CROSS JOIN season_weeks sw
    ),
    week_completion AS (
        SELECT week, bool_and(game_completed) AS all_completed FROM team_weeks GROUP BY week
    ),
    snaps_x AS (
        SELECT s.*, x.gsis_id, x.espn_player_id
        FROM snaps s
        LEFT JOIN xwalk x ON x.pfr_id = s.pfr_player_id
        WHERE s.game_type = 'REG' AND s.position IN ({skill_list})
    ),
    weekly AS (
        SELECT
            sx.gsis_id, sx.week, sx.player AS player_name, sx.position, sx.team,
            sx.opponent, sx.offense_snaps, sx.offense_pct, sx.espn_player_id,
            COALESCE(st.targets, 0) AS targets,
            COALESCE(st.target_share, 0) AS target_share,
            COALESCE(st.air_yards_share, 0) AS air_yards_share,
            COALESCE(st.wopr, 0) AS wopr,
            ROW_NUMBER() OVER (PARTITION BY sx.gsis_id ORDER BY sx.week) AS played_seq
        FROM snaps_x sx
        LEFT JOIN stats st
          ON st.player_id = sx.gsis_id AND st.season = {season} AND st.week = sx.week AND st.season_type = 'REG'
        WHERE sx.gsis_id IS NOT NULL
    ),
    trend AS (
        SELECT
            *,
            offense_pct - LAG(offense_pct, 1) OVER w AS snap_pct_delta_1w,
            offense_pct - LAG(offense_pct, 3) OVER w AS snap_pct_delta_3w,
            CASE WHEN played_seq >= 3 THEN REGR_SLOPE(offense_pct, played_seq) OVER frame ELSE NULL END AS snap_pct_trend
        FROM weekly
        WINDOW
            w AS (PARTITION BY gsis_id ORDER BY week),
            frame AS (PARTITION BY gsis_id ORDER BY week ROWS BETWEEN 2 PRECEDING AND CURRENT ROW)
    ),
    player_span AS (
        -- one row per player: their team for the season (mode, in case a
        -- late trade moves them mid-season) and the first/last week they
        -- actually recorded snaps. This span is what keeps a not-yet-signed
        -- or already-released player's weeks out of the table entirely,
        -- rather than surfacing them as false byes/inactives.
        SELECT gsis_id, mode(team) AS team, MIN(week) AS first_week, MAX(week) AS last_week
        FROM weekly GROUP BY gsis_id
    ),
    player_weeks AS (
        SELECT ps.gsis_id, fg.week, fg.team, tw.game_completed, (tw.team IS NULL) AS bye
        FROM player_span ps
        JOIN full_grid fg
          ON fg.team = ps.team AND fg.week BETWEEN ps.first_week AND ps.last_week
        LEFT JOIN team_weeks tw ON tw.team = fg.team AND tw.week = fg.week
    ),
    player_attrs AS (
        SELECT gsis_id,
               arg_max(player_name, week) AS player_name,
               arg_max(position, week) AS position,
               arg_max(espn_player_id, week) AS espn_player_id
        FROM weekly GROUP BY gsis_id
    )
    SELECT
        pw.gsis_id, {season} AS season, pw.week,
        pa.player_name, pa.position, pw.team,
        t.opponent, pa.espn_player_id,
        (t.gsis_id IS NOT NULL) AS played,
        pw.bye,
        pw.game_completed,
        t.offense_snaps, t.offense_pct,
        t.targets, t.target_share, t.air_yards_share, t.wopr,
        CASE WHEN t.offense_snaps IS NULL OR t.offense_snaps = 0 THEN NULL
             ELSE t.targets / t.offense_snaps END AS targets_per_snap,
        t.snap_pct_delta_1w, t.snap_pct_delta_3w, t.snap_pct_trend,
        inj.report_status, inj.practice_status,
        dc.pos_rank,
        NOT wc.all_completed AS provisional
    FROM player_weeks pw
    JOIN player_attrs pa ON pa.gsis_id = pw.gsis_id
    LEFT JOIN trend t ON t.gsis_id = pw.gsis_id AND t.week = pw.week
    LEFT JOIN week_completion wc ON wc.week = pw.week
    LEFT JOIN injuries inj ON inj.gsis_id = pw.gsis_id AND inj.week = pw.week AND inj.season = {season}
    LEFT JOIN depth_charts dc ON dc.gsis_id = pw.gsis_id
    ORDER BY pw.week, pw.gsis_id
    """
    return con.execute(query).df()
