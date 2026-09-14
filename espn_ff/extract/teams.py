"""mTeam -> one row per team."""

import pandas as pd

from ._common import members_index, owner_names, team_name


def teams_frame(payload):
    members = members_index(payload)
    rows = []
    for team in payload.get("teams") or []:
        overall = (team.get("record") or {}).get("overall") or {}
        rows.append(
            {
                "team_id": team.get("id"),
                "team_name": team_name(team),
                "abbrev": team.get("abbrev"),
                "owner": owner_names(team, members),
                "wins": overall.get("wins"),
                "losses": overall.get("losses"),
                "ties": overall.get("ties"),
                "points_for": overall.get("pointsFor"),
                "points_against": overall.get("pointsAgainst"),
                "percentage": overall.get("percentage"),
                "playoff_seed": team.get("playoffSeed"),
                "draft_day_projected_rank": team.get("draftDayProjectedRank"),
                "waiver_rank": team.get("waiverRank"),
            }
        )
    return pd.DataFrame(rows).sort_values("team_id", ignore_index=True)
