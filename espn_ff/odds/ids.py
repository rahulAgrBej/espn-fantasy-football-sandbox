"""The name join, and its warning label.

The Odds API ships no player ids at all -- a player prop's `description`
field is a full name (e.g. "Jayden Reed") and nothing else, and a game's
odds response doesn't say which of the two teams that player is on either.
Every other id join in this repo (espn_ff/sleeper/ids.py,
espn_ff/nflverse/ids.py) has a real id to anchor on and falls back to a
name match only for the rows an id join misses. This is the one place in
the repo where a name join is unavoidable for every row, and the one place
`team` is a hint rather than a guaranteed key: jobs.py fills it in from the
roster walk it already does to choose which markets to request (handoff
§8), but a prop on the *opposing* team's player -- surfaced by a
game-level market like player_anytime_td -- has no such hint, so this
matches on name first and uses `team` only to break a tie when the name
alone is ambiguous.

Reuses espn_ff/names.py's normalizers rather than writing new ones. Source
order mirrors espn_ff/sleeper/ids.py:resolve -- player_xwalk.csv, then
player_id_map.csv, then the raw ESPN player pool. Unmatched rows are kept,
not dropped: a prop on a just-signed player is exactly the signal this
layer exists to surface, so this follows the nflverse warn-don't-raise
precedent (espn_ff/cli.py:348-355), not Sleeper's raise.
"""

import pandas as pd

from ..names import normalize_name, normalize_team

MATCH_SOURCES = ("xwalk", "sleeper_map", "espn_pool", "unmatched")


def _add(index, name, team, espn_id):
    index.setdefault(name, []).append((team, espn_id))


def _xwalk_index(xwalk_df):
    """normalized name -> [(team, espn_player_id), ...], from
    player_xwalk.csv (display_name + latest_team, its only name-shaped
    column pair)."""
    index = {}
    if xwalk_df is None or xwalk_df.empty:
        return index
    for _, row in xwalk_df.iterrows():
        if pd.isna(row.get("espn_player_id")):
            continue
        _add(index, normalize_name(row.get("display_name")), normalize_team(row.get("latest_team")), row["espn_player_id"])
    return index


def _sleeper_map_index(sleeper_map_df, espn_players_df):
    """normalized name -> [(team, espn_player_id), ...], from
    player_id_map.csv resolved through the ESPN pool -- the map itself only
    carries sleeper_id -> espn_player_id, no name."""
    index = {}
    if sleeper_map_df is None or sleeper_map_df.empty or espn_players_df is None or espn_players_df.empty:
        return index
    espn_by_id = espn_players_df.set_index("player_id")
    for _, row in sleeper_map_df.iterrows():
        espn_id = row.get("espn_player_id")
        if pd.isna(espn_id) or espn_id not in espn_by_id.index:
            continue
        espn_row = espn_by_id.loc[espn_id]
        _add(index, normalize_name(espn_row.get("player_name")), normalize_team(espn_row.get("pro_team")), espn_id)
    return index


def _espn_pool_index(espn_players_df):
    index = {}
    if espn_players_df is None or espn_players_df.empty:
        return index
    for _, row in espn_players_df.iterrows():
        _add(index, normalize_name(row.get("player_name")), normalize_team(row.get("pro_team")), row["player_id"])
    return index


def _lookup(index, name, team):
    """One name may map to several (team, id) pairs (a common surname
    across two rosters). Exactly one candidate overall resolves
    unambiguously; more than one requires `team` to narrow it to exactly
    one -- an ambiguous, un-narrowable name is unmatched, never a guess."""
    candidates = index.get(name)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][1]
    if team:
        narrowed = [espn_id for cand_team, espn_id in candidates if cand_team == team]
        if len(narrowed) == 1:
            return narrowed[0]
    return None


def resolve(props_df, xwalk_df=None, sleeper_map_df=None, espn_players_df=None):
    """props_df carries at least `player_name`, and optionally `team` (the
    Odds API's full team name, or None where jobs.py could not infer it
    from the roster walk -- normalized the same as everywhere else via
    names.TEAM_ALIASES). Returns (props_df, unmatched) where props_df has
    two new columns -- `espn_player_id` and `match_source` -- and
    `unmatched` is the list of row indices that resolved by no path.

    Every row is kept, always -- match_source == "unmatched" rows are
    reported by the caller, never dropped.
    """
    xwalk_index = _xwalk_index(xwalk_df)
    sleeper_index = _sleeper_map_index(sleeper_map_df, espn_players_df)
    espn_index = _espn_pool_index(espn_players_df)

    out = props_df.copy()
    espn_ids, sources, unmatched = [], [], []

    for idx, row in out.iterrows():
        name = normalize_name(row.get("player_name"))
        team = normalize_team(row.get("team")) if pd.notna(row.get("team")) else None

        for source_name, index in (("xwalk", xwalk_index), ("sleeper_map", sleeper_index), ("espn_pool", espn_index)):
            espn_id = _lookup(index, name, team)
            if espn_id is not None:
                espn_ids.append(espn_id)
                sources.append(source_name)
                break
        else:
            espn_ids.append(None)
            sources.append("unmatched")
            unmatched.append(idx)

    out["espn_player_id"] = espn_ids
    out["match_source"] = sources
    return out, unmatched
