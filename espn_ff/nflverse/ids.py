"""ESPN <-> nflverse identity resolution.

`gsis_id` is the internal primary key everywhere in this package; `espn_id`
is translated only at the ESPN boundary. Verified against the live 2026
pool: of 181 week-1 roster rows, 168 matched `players.espn_id` directly and
all 13 misses were D/ST, which nflverse carries no player row for by design
(a team defence is not a player). Real orphans that week: zero.

Unlike espn_ff/sleeper/ids.py, this module does no name matching -- nflverse
ships `espn_id` directly on `players.parquet`, and where that is null,
dynastyprocess's db_playerids.csv (fetched from raw.githubusercontent.com,
not a GitHub release asset) is a second id-keyed source. A player resolvable
by neither path is logged as unresolved rather than fuzzy-matched by name.
"""

from io import StringIO

import pandas as pd

from .client import NflverseClient

XWALK_COLUMNS = [
    "gsis_id", "pfr_id", "espn_player_id", "display_name", "position",
    "latest_team", "status", "source",
]

DB_PLAYERIDS_URL = "https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_playerids.csv"


def fetch_fallback(client=None):
    """db_playerids.csv -> DataFrame[gsis_id, espn_id]. Best-effort: any
    failure returns an empty frame rather than raising, since this is only a
    backfill for players.parquet's own espn_id gaps, not a required feed."""
    try:
        client = client or NflverseClient()
        text = client.download_text(DB_PLAYERIDS_URL)
        df = pd.read_csv(StringIO(text), dtype=str)
    except Exception as exc:  # noqa: BLE001 -- fallback is best-effort, never fatal
        print(f"  [warning] db_playerids fallback fetch failed: {exc}")
        return pd.DataFrame(columns=["gsis_id", "espn_id"])
    return df[["gsis_id", "espn_id"]].dropna(subset=["gsis_id"])


def build_xwalk(players_df, fallback_df=None):
    """players.parquet (+ optional db_playerids fallback) -> (xwalk_df, stats).

    One row per gsis_id, source="players". Rows whose espn_id is null are
    backfilled from `fallback_df` (matched on gsis_id) with
    source="db_playerids". Never drops a row for lacking an espn_id -- the
    feature table still needs the gsis_id row; it just won't join back to an
    ESPN roster.
    """
    df = players_df.dropna(subset=["gsis_id"]).drop_duplicates(subset=["gsis_id"]).copy()
    df["source"] = "players"

    if fallback_df is not None and not fallback_df.empty:
        fb = (
            fallback_df.dropna(subset=["gsis_id"])
            .drop_duplicates(subset=["gsis_id"])
            .rename(columns={"espn_id": "espn_id_fallback"})
        )
        df = df.merge(fb[["gsis_id", "espn_id_fallback"]], on="gsis_id", how="left")
        needs_fill = df["espn_id"].isna() & df["espn_id_fallback"].notna()
        df.loc[needs_fill, "espn_id"] = df.loc[needs_fill, "espn_id_fallback"]
        df.loc[needs_fill, "source"] = "db_playerids"
        df = df.drop(columns=["espn_id_fallback"])

    df["espn_player_id"] = pd.to_numeric(df["espn_id"], errors="coerce").astype("Int64")
    for col in ("pfr_id", "display_name", "position", "latest_team", "status"):
        if col not in df.columns:
            df[col] = None

    xwalk = df[XWALK_COLUMNS].reset_index(drop=True)
    stats = {
        "total": len(xwalk),
        "espn_matched": int(xwalk["espn_player_id"].notna().sum()),
        "from_players": int((xwalk["source"] == "players").sum()),
        "from_db_playerids": int((xwalk["source"] == "db_playerids").sum()),
    }
    return xwalk, stats


def orphans(left_df, xwalk_df, on, label_cols):
    """Rows in `left_df` whose `on` key has no match in xwalk_df[on].
    Reported, never dropped -- the caller excludes D/ST before calling this,
    since nflverse has no player row for a team defence and counting all of
    them every week would bury the one real orphan that matters."""
    matched_keys = set(xwalk_df[on].dropna()) if not xwalk_df.empty else set()
    mask = ~left_df[on].isin(matched_keys)
    cols = [on] + [c for c in label_cols if c in left_df.columns]
    return left_df.loc[mask, cols].reset_index(drop=True)
