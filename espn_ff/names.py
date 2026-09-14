"""Name/team/position normalisation shared by every cross-source id join.

Promoted out of espn_ff/sleeper/ids.py so the nflverse crosswalk (which needs
the same team-alias table, plus a few historic entries Sleeper never needed)
doesn't reimplement it. espn_ff/sleeper/ids.py re-exports everything here so
existing call sites and tests are untouched.
"""

import re

import pandas as pd

TEAM_ALIASES = {
    "WAS": "WSH",
    "JAC": "JAX",
    "LA": "LAR",
    # Historic realignments -- only relevant to callers reaching into
    # backfill seasons (nflverse's players.parquet/games.parquet go back to
    # 1999 and use each team's contemporaneous abbreviation).
    "OAK": "LV",
    "SD": "LAC",
    "STL": "LAR",
}

POSITION_ALIASES = {
    "DEF": "D/ST",
}

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


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
