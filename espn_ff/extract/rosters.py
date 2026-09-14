"""mRoster -> one row per (team, week, player).

mRoster returns the roster *as of* the requested scoringPeriodId, so a season
history means one fetch per week.
"""

import pandas as pd

from .. import constants, stats
from ._common import team_name


def rosters_frame(payload, season, week):
    rows = []
    for team in payload.get("teams") or []:
        entries = ((team.get("roster") or {}).get("entries")) or []
        for entry in entries:
            player = ((entry.get("playerPoolEntry") or {}).get("player")) or {}
            slot_id = entry.get("lineupSlotId")
            rows.append(
                {
                    "season": season,
                    "week": week,
                    "team_id": team.get("id"),
                    "team_name": team_name(team),
                    "player_id": player.get("id"),
                    "player_name": player.get("fullName"),
                    "position": constants.position(player.get("defaultPositionId")),
                    "pro_team": constants.pro_team(player.get("proTeamId")),
                    "lineup_slot_id": slot_id,
                    "lineup_slot": constants.slot(slot_id),
                    "started": slot_id not in constants.NON_SCORING_SLOTS,
                    "injury_status": player.get("injuryStatus"),
                    "acquisition_type": entry.get("acquisitionType"),
                    "points": stats.weekly_points(player, season=season, week=week),
                    "projected": stats.weekly_points(
                        player, season=season, week=week, projected=True
                    ),
                }
            )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values(
        ["team_id", "started", "lineup_slot_id"],
        ascending=[True, False, True],
        ignore_index=True,
    )
