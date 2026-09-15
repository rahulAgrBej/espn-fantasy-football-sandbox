"""The three-source availability read: ESPN, Sleeper, nflverse, joined and
**shown side by side, never silently collapsed** into a single verdict.

| Source   | Fields                                                              |
|----------|----------------------------------------------------------------------|
| ESPN     | `injury_status` on weekly-rosters.csv                                |
| Sleeper  | `injury_status`, `injury_body_part`, `injury_notes`,                  |
|          | `practice_participation` from the latest daily slim snapshot         |
| nflverse | `report_status`, `practice_status`, `report_primary_injury`          |

Joins: Sleeper via data/sleeper/player_id_map.csv (espn_ff/sleeper/ids.py's
map); nflverse via data/nflverse/player_xwalk.csv (espn_ff/nflverse/ids.py's
crosswalk) on gsis_id, keyed (season, week, gsis_id) against the injuries
dataset. `sleeper_signals.availability_tier` is reused as-is -- this module
does not define a second tier function.

Precedence when sources disagree, stated in the rendered report: nflverse
`report_status` (the official, week-keyed designation) -> Sleeper `tier` ->
ESPN `injury_status`. All three sources render regardless of which one
wins the `tier` column, so a disagreement is visible rather than resolved
behind the reader's back.

Null handling: `report_status` is null both when the injuries feed is dark
and when a player carries no designation. Those two are told apart by
checking whether the (season, week) slice has *any* rows at all -- a dark
feed (or a week that hasn't been published yet) renders `tier` as
"insufficient data"; a real absence of rows for one player, in a week that
otherwise has data, means that player has no designation and falls through
to Sleeper/ESPN.
"""

import pandas as pd

from .. import config
from ..nflverse import store as nflverse_store
from ..sleeper import signals as sleeper_signals
from ..sleeper import snapshots as sleeper_snapshots

INSUFFICIENT_DATA = "insufficient data"


def _latest_sleeper_slim():
    dates = sleeper_snapshots.list_slim_dates()
    if not dates:
        return pd.DataFrame()
    return sleeper_snapshots.read_slim(dates[-1])


def _id_map():
    if not config.SLEEPER_ID_MAP.exists():
        return pd.DataFrame(columns=["sleeper_id", "espn_player_id", "source"])
    return pd.read_csv(config.SLEEPER_ID_MAP)


def _xwalk():
    if not config.NFLVERSE_XWALK.exists():
        return pd.DataFrame(columns=["gsis_id", "espn_player_id"])
    return pd.read_csv(config.NFLVERSE_XWALK, dtype={"gsis_id": str})


def _sleeper_by_espn_id():
    id_map, slim = _id_map(), _latest_sleeper_slim()
    if id_map.empty or slim.empty:
        return pd.DataFrame()
    merged = id_map.merge(slim, on="sleeper_id", how="inner")
    return merged.dropna(subset=["espn_player_id"]).assign(
        espn_player_id=lambda d: d["espn_player_id"].astype(int)
    )


def _xwalk_by_espn_id():
    xwalk = _xwalk()
    if xwalk.empty:
        return xwalk
    return xwalk.dropna(subset=["espn_player_id"]).assign(
        espn_player_id=lambda d: d["espn_player_id"].astype(int)
    )


def read(players_df, season, week):
    """`players_df` needs `player_id`, `player_name`, `injury_status`
    (ESPN's) columns -- e.g. a weekly-rosters.csv slice. Returns one row
    per input player with every source's raw fields plus a resolved
    `tier`."""
    sleeper = _sleeper_by_espn_id()
    xwalk = _xwalk_by_espn_id()

    injuries = nflverse_store.load("injuries", season=season)
    week_slice = injuries[(injuries["season"] == season) & (injuries["week"] == week)] if not injuries.empty else injuries
    week_has_data = not week_slice.empty

    rows = []
    for _, player in players_df.iterrows():
        player_id = player["player_id"]

        s_match = sleeper[sleeper["espn_player_id"] == player_id] if not sleeper.empty else pd.DataFrame()
        s_injury_status = s_match["injury_status"].iloc[0] if not s_match.empty else None
        s_body_part = s_match["injury_body_part"].iloc[0] if not s_match.empty else None
        s_notes = s_match["injury_notes"].iloc[0] if not s_match.empty else None
        s_practice = s_match["practice_participation"].iloc[0] if not s_match.empty else None
        sleeper_tier = (
            sleeper_signals.availability_tier(s_injury_status, s_practice) if not s_match.empty else None
        )

        report_status = practice_status = report_injury = None
        if not xwalk.empty and week_has_data:
            gsis_match = xwalk[xwalk["espn_player_id"] == player_id]
            if not gsis_match.empty:
                gsis_id = gsis_match["gsis_id"].iloc[0]
                n_row = week_slice[week_slice["gsis_id"] == gsis_id]
                if not n_row.empty:
                    report_status = n_row["report_status"].iloc[0]
                    practice_status = n_row["practice_status"].iloc[0]
                    report_injury = n_row["report_primary_injury"].iloc[0]

        if not week_has_data:
            tier = INSUFFICIENT_DATA
        elif pd.notna(report_status) and report_status:
            tier = report_status
        elif sleeper_tier:
            tier = sleeper_tier
        else:
            tier = player.get("injury_status") or "CLEAR"

        rows.append(
            {
                "player_id": player_id,
                "player_name": player.get("player_name"),
                "pro_team": player.get("pro_team"),
                "espn_injury_status": player.get("injury_status"),
                "sleeper_injury_status": s_injury_status,
                "sleeper_injury_body_part": s_body_part,
                "sleeper_injury_notes": s_notes,
                "sleeper_practice_participation": s_practice,
                "sleeper_tier": sleeper_tier,
                "nflverse_report_status": report_status,
                "nflverse_practice_status": practice_status,
                "nflverse_report_primary_injury": report_injury,
                "tier": tier,
            }
        )
    return pd.DataFrame(rows)
