"""On-disk store for the Odds API layer: raw archive, append-only snapshot
parquet, league-scoring snapshot, and last_run staleness.

No `_prune` function exists in this module, and that is the point. Every
other feed in this repo (see `espn_ff/sleeper/snapshots.py`'s `KEEP_SLIM`)
can be re-fetched for free, so pruning old snapshots costs nothing to
reverse. This layer cannot: `/v4/historical/*` is paid-tier and blocked at
the client, so the only odds history this project will ever have is
whatever `archive_raw`/`append_snapshot` wrote at fetch time. Add a pruning
function here later only with a durable secondary copy of what it deletes.
"""

import json
import os
import time
from pathlib import Path

import pandas as pd

from .. import config

# `outcome_name` is load-bearing on both: a two-sided market (Over/Under,
# Yes/No) puts both sides in the same (event_id, .../market, book) tuple at
# the same captured_at, and consensus_line's cross-book median needs both --
# omitting outcome_name here let one side silently clobber the other on
# every real capture (verified against a live team_totals.parquet: the
# totals market's team=None left Over and Under identical on every column
# but outcome_name, and only the last-written side survived).
PROPS_DEDUPE_KEYS = ["captured_at", "event_id", "player_name", "market", "book", "outcome_name"]
TOTALS_DEDUPE_KEYS = ["captured_at", "event_id", "team", "market", "book", "outcome_name"]


def raw_path(billing_period, job_run_id, endpoint, event_id, ts):
    event_segment = str(event_id) if event_id is not None else "all"
    return (
        config.ODDS_RAW_DIR / str(billing_period) / str(job_run_id) / endpoint / event_segment / f"{ts}.json"
    )


def archive_raw(raw_text, billing_period, job_run_id, endpoint, event_id, ts):
    """Persist the exact response body before any parsing happens -- the
    `.partial` + os.replace swap already used by
    espn_ff/nflverse/client.py:82-85, so a killed write never leaves a
    truncated archive where a complete one used to be. Takes the response's
    raw text verbatim, not a re-serialized object, so a later JSON parse
    failure can never mean the archive itself is incomplete or absent."""
    dest = raw_path(billing_period, job_run_id, endpoint, event_id, ts)
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".partial")
    partial.write_text(raw_text)
    os.replace(partial, dest)
    return dest


def _atomic_write_parquet(df, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    df.to_parquet(partial, index=False)
    os.replace(partial, path)


def append_snapshot(df, path, dedupe_keys):
    """Read-concat-dedupe against whatever is already at `path`, then
    rewrite atomically. Never truncates -- a snapshot already on disk is
    never a candidate for deletion here, only for being superseded by a
    later capture of the same (event, player/team, market, book)."""
    if df is None or df.empty:
        return pd.read_parquet(path) if path.exists() else df

    existing = pd.read_parquet(path) if path.exists() else None
    combined = pd.concat([existing, df], ignore_index=True) if existing is not None else df
    combined = combined.drop_duplicates(subset=dedupe_keys, keep="last").reset_index(drop=True)
    _atomic_write_parquet(combined, path)
    return combined


def write_league_scoring(scoring_dict):
    config.ODDS_DIR.mkdir(parents=True, exist_ok=True)
    config.ODDS_SCORING.write_text(json.dumps(scoring_dict, indent=2, default=str))


def read_league_scoring():
    if not config.ODDS_SCORING.exists():
        return None
    return json.loads(config.ODDS_SCORING.read_text())


def last_run_path():
    return config.ODDS_DIR / "last_run.json"


def write_last_run(job, credits_spent, stale, reason=None, ran_at=None):
    """Same shape and role as sleeper/snapshots.py:_write_last_run --
    including the stale/reason pair. Keyed by job name so one job's
    (e.g. props_primary's) staleness never masks another's."""
    config.ODDS_DIR.mkdir(parents=True, exist_ok=True)
    path = last_run_path()
    all_runs = json.loads(path.read_text()) if path.exists() else {}
    all_runs[job] = {
        "job": job,
        "ran_at": ran_at if ran_at is not None else time.time(),
        "credits_spent": credits_spent,
        "stale": stale,
        "reason": reason,
    }
    path.write_text(json.dumps(all_runs, indent=2, default=str))


def read_last_run(job=None):
    path = last_run_path()
    if not path.exists():
        return None
    all_runs = json.loads(path.read_text())
    return all_runs.get(job) if job else all_runs
