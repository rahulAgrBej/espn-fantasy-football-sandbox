"""mMatchupScore -> one row per (week, matchup, team side).

Two traps live in this payload. During an in-progress week `totalPoints` is
0.0 and the real number sits in `totalPointsLive`, so a naive read scores every
live matchup as a 0-0 tie. And `winner` is authoritative ("UNDECIDED" while a
week is open), so the result is taken from it rather than inferred by comparing
two placeholder zeros.
"""

import pandas as pd

from ._common import team_name, teams_index

WINNER_SIDE = {"HOME": "home", "AWAY": "away"}


def _points(entry):
    """Final points when the week has closed, live points while it is open."""
    total = entry.get("totalPoints") or 0.0
    live = entry.get("totalPointsLive")
    return live if (not total and live) else total


def matchups_frame(payload, season):
    teams = teams_index(payload)
    rows = []
    for game in payload.get("schedule") or []:
        winner = game.get("winner")
        for side in ("home", "away"):
            entry = game.get(side)
            if not entry:
                continue  # bye week
            opponent = game.get("away" if side == "home" else "home") or {}
            team_id = entry.get("teamId")
            rows.append(
                {
                    "season": season,
                    "week": game.get("matchupPeriodId"),
                    "matchup_id": game.get("id"),
                    "playoff_tier": game.get("playoffTierType"),
                    "side": side,
                    "team_id": team_id,
                    "team_name": team_name(teams.get(team_id, {})),
                    "points": _points(entry),
                    "points_final": entry.get("totalPoints"),
                    "points_live": entry.get("totalPointsLive"),
                    "projected": entry.get("totalProjectedPoints"),
                    "opponent_id": opponent.get("teamId"),
                    "opponent_name": team_name(teams.get(opponent.get("teamId"), {}))
                    if opponent
                    else None,
                    "opponent_points": _points(opponent) if opponent else None,
                    "winner": winner,
                    "result": _result(winner, side),
                }
            )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values(["week", "matchup_id", "side"], ignore_index=True)


def _result(winner, side):
    """W/L/T from ESPN's own verdict; None while the week is still open."""
    if winner == "TIE":
        return "T"
    if winner in WINNER_SIDE:
        return "W" if WINNER_SIDE[winner] == side else "L"
    return None  # UNDECIDED or absent
