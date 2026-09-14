"""ID -> label lookups.

The small lineup/position maps are stable enough to inline. The 235-entry stat
dictionary and the statSource/statSplit lookups are pulled from ESPN's own
public platform-settings endpoint so they never drift out of sync.
"""

from functools import lru_cache

LINEUP_SLOTS = {
    0: "QB", 1: "TQB", 2: "RB", 3: "RB/WR", 4: "WR", 5: "WR/TE", 6: "TE",
    7: "OP", 8: "DT", 9: "DE", 10: "LB", 11: "DL", 12: "CB", 13: "S",
    14: "DB", 15: "DP", 16: "D/ST", 17: "K", 18: "P", 19: "HC",
    20: "Bench", 21: "IR", 23: "FLEX", 24: "ER",
}

# Slots whose points do not count toward a team's weekly total.
NON_SCORING_SLOTS = {20, 21, 24}

POSITIONS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "D/ST"}

PRO_TEAMS = {
    0: "FA", 1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL",
    7: "DEN", 8: "DET", 9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV",
    14: "LAR", 15: "MIA", 16: "MIN", 17: "NE", 18: "NO", 19: "NYG",
    20: "NYJ", 21: "PHI", 22: "ARI", 23: "PIT", 24: "LAC", 25: "SF",
    26: "SEA", 27: "TB", 28: "WSH", 29: "CAR", 30: "JAX", 33: "BAL",
    34: "HOU",
}

# ESPN's transaction/roster-entry status strings are passed through as-is.
ACQUISITION_TYPES = {
    "DRAFT": "draft", "ADD": "add", "TRADE": "trade", "WAIVER": "waiver",
}


@lru_cache(maxsize=None)
def stat_dictionary(season=None):
    """{stat_id: abbrev} for all 235 stats, from the public endpoint."""
    from .client import EspnClient

    client = EspnClient(season=season) if season else EspnClient()
    stats = client.get_platform_settings().get("settings", {}).get("statSettings", {})
    return {
        s["id"]: s.get("abbrev") or s.get("name") or str(s["id"])
        for s in stats.get("stats", [])
        if s.get("id") is not None
    }


@lru_cache(maxsize=None)
def stat_lookups(season=None):
    """ESPN's own decode tables for statSourceId and statSplitTypeId."""
    from .client import EspnClient

    client = EspnClient(season=season) if season else EspnClient()
    stats = client.get_platform_settings().get("settings", {}).get("statSettings", {})
    return {
        "sources": stats.get("sources", {}),
        "split_types": stats.get("splitTypes", {}),
    }


def slot(slot_id):
    return LINEUP_SLOTS.get(slot_id, str(slot_id))


def position(position_id):
    return POSITIONS.get(position_id, "?")


def pro_team(team_id):
    return PRO_TEAMS.get(team_id, str(team_id))
