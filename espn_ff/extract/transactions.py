"""mTransactions2 -> one row per transaction item.

The log is item-grained: one transaction can move several players (a trade), so
each item becomes a row. Team id 0 means free agency, not team 0, and a lineup
slot of -1 means "no slot" -- both are normalised to None here.
"""

import pandas as pd

from .. import constants
from ._common import members_index, team_name, teams_index

NO_TEAM = 0
NO_SLOT = -1


def _team(team_id, teams):
    """Resolve a team id, treating 0 as free agency rather than a real team."""
    if not team_id or team_id == NO_TEAM:
        return None, None
    return team_id, team_name(teams.get(team_id, {}))


def _slot(slot_id):
    return None if slot_id in (None, NO_SLOT) else constants.slot(slot_id)


def transactions_frame(payload, season, player_names=None):
    teams = teams_index(payload)
    members = members_index(payload)
    player_names = player_names or {}
    rows = []
    for txn in payload.get("transactions") or []:
        member = members.get(txn.get("memberId")) or {}
        acting_id, acting_name = _team(txn.get("teamId"), teams)
        for item in txn.get("items") or []:
            player_id = item.get("playerId")
            from_id, from_name = _team(item.get("fromTeamId"), teams)
            to_id, to_name = _team(item.get("toTeamId"), teams)
            rows.append(
                {
                    "season": season,
                    "transaction_id": txn.get("id"),
                    "scoring_period": txn.get("scoringPeriodId"),
                    "type": txn.get("type"),
                    "item_type": item.get("type"),
                    "status": txn.get("status"),
                    "execution_type": txn.get("executionType"),
                    "is_pending": txn.get("isPending"),
                    "acting_team_id": acting_id,
                    "acting_team": acting_name,
                    "acting_member": f"{member.get('firstName', '')} "
                    f"{member.get('lastName', '')}".strip()
                    or None,
                    "player_id": player_id,
                    "player_name": player_names.get(player_id),
                    "from_team_id": from_id,
                    "from_team": from_name,
                    "to_team_id": to_id,
                    "to_team": to_name,
                    "from_slot": _slot(item.get("fromLineupSlotId")),
                    "to_slot": _slot(item.get("toLineupSlotId")),
                    "bid_amount": txn.get("bidAmount"),
                    "is_keeper": item.get("isKeeper"),
                    "proposed_date_ms": txn.get("proposedDate"),
                }
            )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["proposed_date"] = pd.to_datetime(df["proposed_date_ms"], unit="ms", errors="coerce")
    return df.sort_values(
        ["scoring_period", "proposed_date_ms"], ignore_index=True
    )
