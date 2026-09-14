"""mDraftDetail -> one row per pick."""

import pandas as pd

from .. import constants
from ._common import members_index, team_name, teams_index

# draftDetail.picks[].autoDraftTypeId
AUTO_DRAFT_TYPES = {0: "manual", 1: "auto", 2: "auto_offline", 3: "autopick"}


def draft_frame(payload, season, player_names=None):
    teams = teams_index(payload)
    members = members_index(payload)
    player_names = player_names or {}
    detail = payload.get("draftDetail") or {}
    rows = []
    for pick in detail.get("picks") or []:
        team_id = pick.get("teamId")
        player_id = pick.get("playerId")
        member = members.get(pick.get("memberId")) or {}
        auto_id = pick.get("autoDraftTypeId")
        rows.append(
            {
                "season": season,
                "overall_pick": pick.get("overallPickNumber"),
                "round": pick.get("roundId"),
                "round_pick": pick.get("roundPickNumber"),
                "team_id": team_id,
                "team_name": team_name(teams.get(team_id, {})),
                "drafted_by": f"{member.get('firstName', '')} {member.get('lastName', '')}".strip()
                or None,
                "player_id": player_id,
                "player_name": player_names.get(player_id),
                "lineup_slot": constants.slot(pick.get("lineupSlotId")),
                "bid_amount": pick.get("bidAmount"),
                "nominating_team_id": pick.get("nominatingTeamId"),
                "keeper": pick.get("keeper"),
                "reserved_for_keeper": pick.get("reservedForKeeper"),
                "auto_draft_type": AUTO_DRAFT_TYPES.get(auto_id, auto_id),
            }
        )
    df = pd.DataFrame(rows)
    return df if df.empty else df.sort_values("overall_pick", ignore_index=True)
