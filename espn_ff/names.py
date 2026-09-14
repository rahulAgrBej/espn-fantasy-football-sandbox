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
    # The Odds API's `home_team`/`away_team`/prop `team` fields carry full
    # team names, not an abbreviation -- espn_ff/odds/ids.py is the only
    # caller that needs these; normalize_team() uppercases first, so the
    # keys below are upper-cased full names, not title case.
    "ARIZONA CARDINALS": "ARI",
    "ATLANTA FALCONS": "ATL",
    "BALTIMORE RAVENS": "BAL",
    "BUFFALO BILLS": "BUF",
    "CAROLINA PANTHERS": "CAR",
    "CHICAGO BEARS": "CHI",
    "CINCINNATI BENGALS": "CIN",
    "CLEVELAND BROWNS": "CLE",
    "DALLAS COWBOYS": "DAL",
    "DENVER BRONCOS": "DEN",
    "DETROIT LIONS": "DET",
    "GREEN BAY PACKERS": "GB",
    "HOUSTON TEXANS": "HOU",
    "INDIANAPOLIS COLTS": "IND",
    "JACKSONVILLE JAGUARS": "JAX",
    "KANSAS CITY CHIEFS": "KC",
    "LAS VEGAS RAIDERS": "LV",
    "LOS ANGELES CHARGERS": "LAC",
    "LOS ANGELES RAMS": "LAR",
    "MIAMI DOLPHINS": "MIA",
    "MINNESOTA VIKINGS": "MIN",
    "NEW ENGLAND PATRIOTS": "NE",
    "NEW ORLEANS SAINTS": "NO",
    "NEW YORK GIANTS": "NYG",
    "NEW YORK JETS": "NYJ",
    "PHILADELPHIA EAGLES": "PHI",
    "PITTSBURGH STEELERS": "PIT",
    "SAN FRANCISCO 49ERS": "SF",
    "SEATTLE SEAHAWKS": "SEA",
    "TAMPA BAY BUCCANEERS": "TB",
    "TENNESSEE TITANS": "TEN",
    "WASHINGTON COMMANDERS": "WSH",
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
