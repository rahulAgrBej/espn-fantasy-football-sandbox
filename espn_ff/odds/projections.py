"""Derived betting-market projections: implied team totals, de-vigged
anytime-TD probability, and prop-line-to-fantasy-points conversion. Pure
and offline -- the sleeper/signals.py and nflverse/features.py analogue.
No network; `build` reads whatever `store.append_snapshot` has already
written to data/odds/*.parquet plus the league_scoring.json snapshot
`slate_context` writes from ESPN's own settings.scoring_frame.

Expected tidy row shape for the raw quotes this module consumes (what
jobs.py flattens an /odds or /events/{id}/odds response into before
calling store.append_snapshot): one row per
(event_id, player_name-or-team, market, book, outcome_name, price, point).
Line/points markets (a numeric `point`, quoted Over/Under) carry a real
`point`; two-way probability markets with no line (player_anytime_td:
Yes/No prices only) carry `point = NaN` and are de-vigged here instead.
"""

import pandas as pd

from .. import config
from . import ids, store
from .markets import MARKET_STATS


def devig_two_way(price_yes, price_no):
    """American odds for both sides of a two-way market -> (p_yes, p_no),
    normalized so they sum to 1. The only correct way to read
    player_anytime_td -- the raw two-sided prices always overstate both
    outcomes' true probability by the book's vig."""

    def _implied_prob(price):
        return 100 / (price + 100) if price > 0 else (-price) / (-price + 100)

    raw_yes, raw_no = _implied_prob(price_yes), _implied_prob(price_no)
    total = raw_yes + raw_no
    return raw_yes / total, raw_no / total


def consensus_line(rows):
    """Cross-book median per (event, player/team, market). A line/points
    market's median is taken over the "Over" quote's `point` (Over and
    Under always share one point); a Yes/No probability market first
    de-vigs each book's own Yes/No pair, then medians the resulting Yes
    probability across books -- written back into `point` so downstream
    code never has to branch on market shape again.

    "spreads" is a third shape, handled separately: `_flatten_featured`
    already splits it one row per team (`team` is part of `group_cols`),
    so each group here is already one side of the line -- `outcome_name`
    is that team's own full name, never "Over"/"Yes", and every row in the
    group already carries that team's signed point straight from the API.
    """
    group_cols = [c for c in ("event_id", "player_name", "team", "market") if c in rows.columns]
    out_rows = []
    for key, group in rows.groupby(group_cols, dropna=False):
        key = key if isinstance(key, tuple) else (key,)
        row = dict(zip(group_cols, key))
        if row.get("market") == "spreads":
            row["point"] = group["point"].median()
        elif group["point"].notna().any():
            over = group[group["outcome_name"].isin(["Over", "Yes"])]
            row["point"] = over["point"].median()
        else:
            yes = group.loc[group["outcome_name"] == "Yes"].set_index("book")["price"]
            no = group.loc[group["outcome_name"] == "No"].set_index("book")["price"]
            probs = [devig_two_way(yes[book], no[book])[0] for book in yes.index if book in no.index]
            row["point"] = pd.Series(probs, dtype="float64").median() if probs else None
        out_rows.append(row)
    return pd.DataFrame(out_rows, columns=group_cols + ["point"])


def implied_team_totals(consensus_df):
    """consensus_df: consensus_line's output for the "spreads" and "totals"
    markets. Spread rows are keyed per (event_id, team) carrying that
    team's own signed line (negative when favored); total rows are keyed
    per event_id only, since the total is game-level. One formula covers
    both sides once joined: implied_team_total = total/2 - spread/2 -- a
    favorite's negative spread ADDS to half the total, an underdog's
    positive spread subtracts from it. This is the DST and game-script
    signal.
    """
    spreads = (
        consensus_df.loc[consensus_df["market"] == "spreads", ["event_id", "team", "point"]]
        .rename(columns={"point": "spread"})
    )
    totals = (
        consensus_df.loc[consensus_df["market"] == "totals", ["event_id", "point"]]
        .rename(columns={"point": "total"})
        .drop_duplicates(subset=["event_id"])
    )
    merged = spreads.merge(totals, on="event_id", how="left")
    merged["implied_team_total"] = merged["total"] / 2 - merged["spread"] / 2
    return merged


def team_totals_by_capture(week=None):
    """implied_team_total per (captured_at, team) -- consensus computed
    WITHIN each capture rather than across all of them, which is what
    build() does. Tuesday's slate and Friday's line_movement write to the
    same parquet under the same dedupe keys (store.TOTALS_DEDUPE_KEYS
    includes captured_at); this is the accessor that reads them apart.
    Returns columns captured_at, team, spread, total, implied_team_total --
    `event_id` is dropped, since a bare 32-hex string is rejected by
    .githooks/pre-commit and Odds API event ids are exactly that shape.
    """
    columns = ["captured_at", "team", "spread", "total", "implied_team_total"]
    if not config.ODDS_TEAM_TOTALS.exists():
        return pd.DataFrame(columns=columns)

    totals = pd.read_parquet(config.ODDS_TEAM_TOTALS)
    if week is not None and "week" in totals.columns:
        totals = totals[totals["week"] == week]
    if totals.empty:
        return pd.DataFrame(columns=columns)

    frames = []
    for captured_at, group in totals.groupby("captured_at"):
        merged = implied_team_totals(consensus_line(group))
        merged["captured_at"] = captured_at
        frames.append(merged[columns])
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=columns)


def prop_to_points(consensus_df, scoring):
    """consensus_df: consensus_line's output for player prop markets.
    scoring: the list of scoring-rule dicts from league_scoring.json
    (settings.scoring_frame's own shape -- stat_id/stat_abbrev/points/...).
    `MARKET_STATS` says, per market, which stat abbreviation it converts
    into and how: "line" multiplies the consensus point by the league's
    own points-per-unit; "prob" (player_anytime_td) multiplies the
    de-vigged probability by the league's own points-per-TD, never a
    hardcoded 6; "points" (player_kicking_points) is already stated in
    fantasy points and passes through unconverted.
    """
    points_by_stat = {row["stat_abbrev"]: row["points"] for row in (scoring or []) if row.get("stat_abbrev")}
    out = consensus_df.copy()
    fantasy_points = []
    for _, row in out.iterrows():
        stat, kind = MARKET_STATS.get(row["market"], (None, None))
        if stat is None or kind == "points":
            fantasy_points.append(row["point"])
        else:
            fantasy_points.append(row["point"] * points_by_stat.get(stat, 0))
    out["fantasy_points"] = fantasy_points
    return out


def _default_xwalk():
    """config.NFLVERSE_XWALK read for `ids._xwalk_index`'s shape
    (display_name/latest_team/espn_player_id), not report/availability.py's
    narrower gsis_id one -- same file, different consumer. Missing file
    degrades to an empty frame with the right columns, never raises."""
    if not config.NFLVERSE_XWALK.exists():
        return pd.DataFrame(columns=["display_name", "latest_team", "espn_player_id"])
    return pd.read_csv(config.NFLVERSE_XWALK)


def _default_sleeper_map():
    if not config.SLEEPER_ID_MAP.exists():
        return pd.DataFrame(columns=["sleeper_id", "espn_player_id", "source"])
    return pd.read_csv(config.SLEEPER_ID_MAP)


def resolve_props(props_points_df, espn_players_df=None, xwalk_df=None, sleeper_map_df=None):
    """Attach `espn_player_id` and `match_source` to consensus prop rows.

    Read-time, not capture-time: player_props.parquet stays an unaltered
    archive keyed on PROPS_DEDUPE_KEYS, and a later crosswalk improvement
    re-resolves captures already written. `consensus_line` groups on
    (event_id, player_name, team, market) and so preserves both columns
    ids.resolve needs.

    Unmatched rows are KEPT, never dropped -- a prop on a just-signed
    player is exactly the signal this layer exists to surface.
    """
    xwalk_df = _default_xwalk() if xwalk_df is None else xwalk_df
    sleeper_map_df = _default_sleeper_map() if sleeper_map_df is None else sleeper_map_df
    return ids.resolve(
        props_points_df, xwalk_df=xwalk_df, sleeper_map_df=sleeper_map_df, espn_players_df=espn_players_df
    )


def build(week=None):
    """The two output frames, built entirely from disk. Returns
    (props_points_df, team_totals_df) -- either may be empty if nothing has
    been captured yet for `week`, which is expected and not an error: this
    layer's history begins the day the first job runs.
    """
    scoring = store.read_league_scoring() or []
    props = pd.read_parquet(config.ODDS_PROPS) if config.ODDS_PROPS.exists() else pd.DataFrame()
    totals = pd.read_parquet(config.ODDS_TEAM_TOTALS) if config.ODDS_TEAM_TOTALS.exists() else pd.DataFrame()

    if week is not None:
        if "week" in props.columns:
            props = props[props["week"] == week]
        if "week" in totals.columns:
            totals = totals[totals["week"] == week]

    props_points = prop_to_points(consensus_line(props), scoring) if not props.empty else pd.DataFrame()
    team_totals_points = implied_team_totals(consensus_line(totals)) if not totals.empty else pd.DataFrame()

    return props_points, team_totals_points
