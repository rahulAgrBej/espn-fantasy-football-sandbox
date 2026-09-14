"""mSettings -> league metadata plus one row per scoring rule."""

import pandas as pd

from .. import constants


def league_meta(payload):
    settings = payload.get("settings") or {}
    roster = settings.get("rosterSettings") or {}
    schedule = settings.get("scheduleSettings") or {}
    return {
        "league_id": payload.get("id"),
        "season": payload.get("seasonId"),
        "league_name": settings.get("name"),
        "current_scoring_period": payload.get("scoringPeriodId"),
        "size": settings.get("size"),
        "is_public": settings.get("isPublic"),
        "matchup_periods": schedule.get("matchupPeriodCount"),
        "playoff_teams": schedule.get("playoffTeamCount"),
        "lineup_slot_counts": roster.get("lineupSlotCounts"),
        "roster_size": sum((roster.get("lineupSlotCounts") or {}).values()) or None,
    }


def scoring_frame(payload, season=None):
    """One row per scoring rule, with the stat abbreviation resolved."""
    dictionary = constants.stat_dictionary(season)
    items = ((payload.get("settings") or {}).get("scoringSettings") or {}).get(
        "scoringItems"
    ) or []
    rows = [
        {
            "stat_id": item.get("statId"),
            "stat_abbrev": dictionary.get(item.get("statId")),
            "points": item.get("points"),
            "is_reverse": item.get("isReverseItem"),
            "points_overrides": item.get("pointsOverrides"),
        }
        for item in items
    ]
    df = pd.DataFrame(rows)
    return df if df.empty else df.sort_values("stat_id", ignore_index=True)


def roster_slots_frame(payload):
    counts = (
        (payload.get("settings") or {}).get("rosterSettings") or {}
    ).get("lineupSlotCounts") or {}
    rows = [
        {"slot_id": int(k), "slot": constants.slot(int(k)), "count": v}
        for k, v in counts.items()
        if v
    ]
    df = pd.DataFrame(rows)
    return df if df.empty else df.sort_values("slot_id", ignore_index=True)
