"""Sleeper <-> ESPN identity resolution.

`espn_id` is the join key when Sleeper carries it, populated for most but not
all relevant players. Where it's missing, or doesn't match a live ESPN
player_id, fall back to normalised name + team + position.

Two alias maps bridge Sleeper's and ESPN's differing vocabularies for the
same team or position -- checked against a live snapshot, not just assumed
from the docs:

- Team: Sleeper's WAS vs ESPN's WSH (espn_ff/constants.py PRO_TEAMS), plus
  JAC/JAX and LA/LAR.
- Position: Sleeper's DEF vs ESPN's D/ST.

Team defenses also need a third path found by checking a live snapshot: the
two platforms don't just spell the position differently, they name the
"player" differently too -- Sleeper's full_name is "Seattle Seahawks",
ESPN's is "Seahawks D/ST". There is exactly one D/ST per team, so those rows
resolve on team + position alone, skipping the name comparison entirely.
"""

import re

import pandas as pd

TEAM_ALIASES = {
    "WAS": "WSH",
    "JAC": "JAX",
    "LA": "LAR",
}

POSITION_ALIASES = {
    "DEF": "D/ST",
}

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}

MAP_COLUMNS = ["sleeper_id", "espn_player_id", "source"]


def _text(value):
    """None/NaN-safe string coercion. A DataFrame cell with no value comes
    back as float NaN, not None -- and NaN is truthy, so a plain `or ""`
    guard silently lets it through."""
    return "" if pd.isna(value) else str(value)


def normalize_name(name):
    name = _text(name).lower().replace("'", "").replace(".", "")
    name = re.sub(r"[^a-z0-9\s]", " ", name)
    tokens = [t for t in name.split() if t not in _SUFFIXES]
    return " ".join(tokens)


def normalize_team(team):
    team = _text(team).strip().upper()
    return TEAM_ALIASES.get(team, team)


def normalize_position(position):
    position = _text(position).strip().upper()
    return POSITION_ALIASES.get(position, position)


def _name_index(espn_players_df):
    """(normalised name, team, position) -> list of ESPN player_ids sharing it."""
    index = {}
    for _, row in espn_players_df.iterrows():
        key = (
            normalize_name(row.get("player_name")),
            normalize_team(row.get("pro_team")),
            normalize_position(row.get("position")),
        )
        index.setdefault(key, []).append(row["player_id"])
    return index


def _dst_index(espn_players_df):
    """team -> list of ESPN player_ids for D/ST rows. Name-free on purpose."""
    index = {}
    for _, row in espn_players_df.iterrows():
        if normalize_position(row.get("position")) != "D/ST":
            continue
        team = normalize_team(row.get("pro_team"))
        index.setdefault(team, []).append(row["player_id"])
    return index


def resolve(sleeper_df, espn_players_df, existing_map=None):
    """Resolve Sleeper players against ESPN's player pool.

    Returns (map_df, unmatched): map_df has columns
    [sleeper_id, espn_player_id, source] with source in
    {"espn_id", "name", "manual"}; unmatched is the list of sleeper_id that
    resolved by no path -- logged by the caller, never silently dropped.

    Rows in `existing_map` with source == "manual" are carried over verbatim
    and never re-resolved, so hand-edits to player_id_map.csv survive a
    re-run.
    """
    if existing_map is None or existing_map.empty:
        manual_rows = []
        manual_ids = set()
    else:
        manual = existing_map[existing_map["source"] == "manual"]
        manual_rows = manual.to_dict("records")
        manual_ids = set(manual["sleeper_id"])

    espn_ids = set(espn_players_df["player_id"]) if not espn_players_df.empty else set()
    name_index = _name_index(espn_players_df) if not espn_players_df.empty else {}
    dst_index = _dst_index(espn_players_df) if not espn_players_df.empty else {}

    rows = list(manual_rows)
    unmatched = []

    for _, srow in sleeper_df.iterrows():
        sleeper_id = srow["sleeper_id"]
        if sleeper_id in manual_ids:
            continue

        matched_id = None

        raw_espn_id = srow.get("espn_id")
        if pd.notna(raw_espn_id):
            try:
                candidate = int(raw_espn_id)
            except (TypeError, ValueError):
                candidate = None
            if candidate in espn_ids:
                rows.append({"sleeper_id": sleeper_id, "espn_player_id": candidate, "source": "espn_id"})
                matched_id = candidate

        if matched_id is None and normalize_position(srow.get("position")) == "D/ST":
            candidates = dst_index.get(normalize_team(srow.get("team")))
            if candidates and len(candidates) == 1:
                rows.append({"sleeper_id": sleeper_id, "espn_player_id": candidates[0], "source": "name"})
                matched_id = candidates[0]

        if matched_id is None:
            key = (
                normalize_name(srow.get("full_name")),
                normalize_team(srow.get("team")),
                normalize_position(srow.get("position")),
            )
            candidates = name_index.get(key)
            if candidates and len(candidates) == 1:
                rows.append({"sleeper_id": sleeper_id, "espn_player_id": candidates[0], "source": "name"})
                matched_id = candidates[0]

        if matched_id is None:
            unmatched.append(sleeper_id)

    return pd.DataFrame(rows, columns=MAP_COLUMNS), unmatched


def missing_from_roster(map_df, roster_espn_ids):
    """ESPN player_ids on a roster that have no entry in the resolved map."""
    if map_df is None or map_df.empty:
        matched = set()
    else:
        matched = set(map_df["espn_player_id"].dropna().astype(int))
    return [pid for pid in roster_espn_ids if pid not in matched]
