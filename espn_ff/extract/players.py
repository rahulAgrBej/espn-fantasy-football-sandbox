"""Player pool (kona_player_info) -> tidy DataFrame. Public, no auth."""

import pandas as pd

from .. import constants, stats


def players_frame(payload, season, week=None):
    """One row per player, with ownership, status and points."""
    rows = []
    for entry in payload.get("players") or []:
        player = entry.get("player") or {}
        ownership = player.get("ownership") or {}
        row = {
            "player_id": player.get("id"),
            "player_name": player.get("fullName"),
            "position": constants.position(player.get("defaultPositionId")),
            "pro_team": constants.pro_team(player.get("proTeamId")),
            "active": player.get("active"),
            "injured": player.get("injured"),
            "injury_status": player.get("injuryStatus"),
            "on_team_id": entry.get("onTeamId"),
            "percent_owned": ownership.get("percentOwned"),
            "percent_started": ownership.get("percentStarted"),
            "eligible_slots": ",".join(
                constants.slot(s) for s in (player.get("eligibleSlots") or [])
            ),
            "season_points": stats.season_points(player, season=season),
            "season_projected": stats.season_points(player, season=season, projected=True),
        }
        if week is not None:
            row["week"] = week
            row["week_points"] = stats.weekly_points(player, season=season, week=week)
            row["week_projected"] = stats.weekly_points(
                player, season=season, week=week, projected=True
            )
        rows.append(row)
    return pd.DataFrame(rows)
