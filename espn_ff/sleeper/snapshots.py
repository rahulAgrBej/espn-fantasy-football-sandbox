"""On-disk store for Sleeper data: raw gz cache, slim CSV snapshots, trending
CSVs, retention, and the last_run staleness flag.

Sleeper asks that /v1/players/nfl be called at most once a day.
`fetch_players` is the only path that calls it, and only after checking
whether today's slim snapshot already exists.

Snapshot filenames use ISO `YYYY-MM-DD`, not the repo's `dd-mm-yyyy`
convention used for user-facing CSVs in data/out/. "The snapshot N days ago"
is a core operation for the practice trajectory and depth-chart-delta
signals, and ISO sorts lexically where dd-mm-yyyy does not.
"""

import gzip
import json
import time
from datetime import date, datetime

import pandas as pd

from .. import config
from .client import SleeperClient

# Guard threshold: a successful fetch of the real player pool returns well
# over 2,000 active players. Anything less signals a bad response, not a
# quiet roster change.
MIN_ACTIVE_PLAYERS = 2000

KEEP_SLIM = 10
KEEP_RAW = 3
KEEP_TRENDING = 10

SLIM_COLUMNS = [
    "sleeper_id", "espn_id", "gsis_id", "full_name", "team", "position",
    "depth_chart_position", "depth_chart_order",
    "injury_status", "injury_body_part", "injury_notes",
    "practice_participation", "status", "active", "number", "years_exp",
    "fetched_at",
]


def _today():
    return date.today()


def raw_path(day):
    return config.SLEEPER_RAW_DIR / f"players-{day:%Y-%m-%d}.json.gz"


def slim_path(day):
    return config.SLEEPER_SLIM_DIR / f"{day:%Y-%m-%d}-players.csv"


def trending_path(day):
    return config.SLEEPER_DIR / f"trending-{day:%Y-%m-%d}.csv"


def last_run_path():
    return config.SLEEPER_DIR / "last_run.json"


def _slim_row(sleeper_id, player, fetched_at):
    full_name = player.get("full_name") or " ".join(
        part for part in (player.get("first_name"), player.get("last_name")) if part
    )
    row = {
        "sleeper_id": sleeper_id,
        "espn_id": player.get("espn_id"),
        "gsis_id": player.get("gsis_id"),
        "full_name": full_name or None,
        "team": player.get("team"),
        "position": player.get("position"),
        "depth_chart_position": player.get("depth_chart_position"),
        "depth_chart_order": player.get("depth_chart_order"),
        "injury_status": player.get("injury_status"),
        "injury_body_part": player.get("injury_body_part"),
        "injury_notes": player.get("injury_notes"),
        "practice_participation": player.get("practice_participation"),
        "status": player.get("status"),
        "active": player.get("active"),
        "number": player.get("number"),
        "years_exp": player.get("years_exp"),
        "fetched_at": fetched_at,
    }
    return row


def slim_players(raw, fetched_at):
    """Raw Sleeper players dict -> slim DataFrame.

    Drops rows that are both inactive and teamless -- retired/practice-squad
    noise that is most of the file and none of what matters here.
    """
    rows = []
    for sleeper_id, player in raw.items():
        if not player.get("active") and not player.get("team"):
            continue
        rows.append(_slim_row(sleeper_id, player, fetched_at))
    return pd.DataFrame(rows, columns=SLIM_COLUMNS)


def _write_last_run(fetched_at, player_count, stale, reason=None):
    config.SLEEPER_DIR.mkdir(parents=True, exist_ok=True)
    last_run_path().write_text(
        json.dumps(
            {
                "fetched_at": fetched_at,
                "player_count": player_count,
                "stale": stale,
                "reason": reason,
            },
            indent=2,
        )
    )


def _latest_slim_before(today):
    """Most recent existing slim snapshot strictly before `today`, or an
    empty frame if none exists yet (day-one cold start)."""
    if not config.SLEEPER_SLIM_DIR.exists():
        return pd.DataFrame(columns=SLIM_COLUMNS)
    candidates = sorted(
        p for p in config.SLEEPER_SLIM_DIR.glob("*-players.csv")
        if p.name < f"{today:%Y-%m-%d}-players.csv"
    )
    if not candidates:
        return pd.DataFrame(columns=SLIM_COLUMNS)
    return pd.read_csv(candidates[-1])


def fetch_players(client=None, refresh=False, today=None):
    """Once-per-day fetch + slim. Returns (df, from_cache).

    Guards the fetch: a failed request, or a slim frame with fewer than
    MIN_ACTIVE_PLAYERS rows, leaves yesterday's snapshot file untouched and
    marks last_run.json stale with a reason, rather than ever letting a bad
    response produce a "no injuries anywhere" week. The returned frame in
    that case is the most recent good snapshot on disk.
    """
    today = today or _today()
    path = slim_path(today)
    if not refresh and path.exists():
        return pd.read_csv(path), True

    client = client or SleeperClient()
    fetched_at = time.time()
    reason = None
    raw = None
    try:
        raw = client.get_players()
    except Exception as exc:  # noqa: BLE001 -- any failure here is a guard case
        reason = f"fetch failed: {exc}"

    df = None
    if raw is not None:
        df = slim_players(raw, fetched_at)
        if len(df) < MIN_ACTIVE_PLAYERS:
            reason = f"only {len(df)} active players (< {MIN_ACTIVE_PLAYERS})"
            df = None

    if df is None:
        _write_last_run(fetched_at, None, stale=True, reason=reason)
        return _latest_slim_before(today), False

    config.SLEEPER_RAW_DIR.mkdir(parents=True, exist_ok=True)
    config.SLEEPER_SLIM_DIR.mkdir(parents=True, exist_ok=True)
    with gzip.open(raw_path(today), "wt") as f:
        json.dump(raw, f)
    df.to_csv(path, index=False)
    _write_last_run(fetched_at, len(df), stale=False)
    _prune()
    return df, False


def fetch_trending(kind, client=None, lookback_hours=24, limit=25):
    client = client or SleeperClient()
    return client.get_trending(kind, lookback_hours=lookback_hours, limit=limit)


def write_trending(adds, drops, today=None):
    today = today or _today()
    rows = [{"player_id": r["player_id"], "count": r["count"], "kind": "add"} for r in adds]
    rows += [{"player_id": r["player_id"], "count": r["count"], "kind": "drop"} for r in drops]
    df = pd.DataFrame(rows, columns=["player_id", "count", "kind"])
    config.SLEEPER_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(trending_path(today), index=False)
    return df


def list_slim_dates():
    """Ascending list of dates for which a slim snapshot exists on disk."""
    if not config.SLEEPER_SLIM_DIR.exists():
        return []
    dates = []
    for p in config.SLEEPER_SLIM_DIR.glob("*-players.csv"):
        try:
            dates.append(datetime.strptime(p.name[:10], "%Y-%m-%d").date())
        except ValueError:
            continue
    return sorted(dates)


def read_slim(day):
    path = slim_path(day)
    return pd.read_csv(path) if path.exists() else None


def _prune_dir(directory, pattern, keep):
    if not directory.exists() or keep is None:
        return
    files = sorted(directory.glob(pattern))
    for stale in files[:-keep] if keep > 0 else files:
        stale.unlink()


def _prune():
    _prune_dir(config.SLEEPER_SLIM_DIR, "*-players.csv", KEEP_SLIM)
    _prune_dir(config.SLEEPER_RAW_DIR, "players-*.json.gz", KEEP_RAW)
    _prune_dir(config.SLEEPER_DIR, "trending-*.csv", KEEP_TRENDING)
