"""Shared loading utilities for every report/*.py module: the latest
data/out/ export per dataset, and per-feed freshness.

Freshness is always read from a feed's own on-disk timestamp record --
data/sleeper/last_run.json, data/nflverse/manifest.json, the ESPN cache's
.meta.json sidecars -- and **never** from a file's mtime. A CSV's mtime
says when a job ran, not when its contents became true; a budget-aborted
or --refresh-less run can leave an old mtime's data looking fresh, and a
restored/archived file can leave a fresh mtime on old data.
"""

import json
import re
import time
from datetime import datetime

import pandas as pd

from .. import config
from ..nflverse import store as nflverse_store
from ..odds import store as odds_store

_DATE_PREFIX = re.compile(r"^(\d{2})-(\d{2})-(\d{4})-")

# Reports never see ESPN data older than this without a stale flag -- there
# is no vendor-published cadence for the cache, so this is a report-side
# choice, not an observed contract (see docs/data-sources.md's ESPN section).
ESPN_STALE_HOURS = 24


def latest_export(name):
    """Load the newest data/out/*-<name>.csv, picked by parsing the
    dd-mm-yyyy date encoded in the filename -- never by mtime. Returns an
    empty DataFrame when no export exists yet."""
    candidates = []
    for path in config.OUT_DIR.glob(f"*-{name}.csv"):
        match = _DATE_PREFIX.match(path.name)
        if not match or path.name[match.end():] != f"{name}.csv":
            continue
        day, month, year = match.groups()
        candidates.append((datetime(int(year), int(month), int(day)), path))
    if not candidates:
        return pd.DataFrame()
    _, path = max(candidates, key=lambda pair: pair[0])
    return pd.read_csv(path)


def _sleeper_freshness():
    path = config.SLEEPER_DIR / "last_run.json"
    if not path.exists():
        return None, True
    data = json.loads(path.read_text())
    return data.get("fetched_at"), bool(data.get("stale"))


def _nflverse_freshness():
    manifest = nflverse_store.read_manifest()
    if not manifest:
        return None, True
    fetched_ats = [entry.get("fetched_at") for entry in manifest.values() if entry.get("fetched_at")]
    if not fetched_ats:
        return None, True
    stale = any(nflverse_store.is_stale(entry) for entry in manifest.values())
    return max(fetched_ats), stale


def espn_view_freshness(views, season=None):
    """`fetched_at` for one specific set of ESPN views, or None when that
    view has never been fetched.

    `_espn_freshness` deliberately answers a *different* question -- "when
    did the ESPN feed last run at all" -- by taking the max across every
    cached view. That makes it wrong for any caller asking whether a
    particular payload is current: a fresh `mMatchupScore` pull masks a
    day-old `mTransactions2` one, which is exactly how the 2026-09-16
    Wednesday report read `stale = False` while rendering waiver outcomes
    from a payload that predated the waiver run (Observed). Do not merge the
    two functions back together.

    The slug is derived through `cache._slug`/`cache.cache_path`'s own
    convention rather than hard-coded: `cache_path` sorts the view list
    before slugging, so a caller reordering its views would silently break a
    literal glob and this would return None."""
    from .. import cache

    season = season or config.SEASON
    season_dir = config.RAW_DIR / str(season)
    if not season_dir.exists():
        return None

    slug = cache._slug("-".join(sorted(views)))
    fetched_ats = []
    for meta_path in season_dir.glob(f"{slug}-*.meta.json"):
        try:
            fetched_ats.append(json.loads(meta_path.read_text()).get("fetched_at"))
        except (json.JSONDecodeError, OSError):
            continue
    fetched_ats = [f for f in fetched_ats if f is not None]
    return max(fetched_ats) if fetched_ats else None


def espn_export_warning():
    """`data/espn/last_export.json`'s reason when the last export shrank, else
    None. `cmd_export` writes it when the merged transaction frame comes out
    smaller than the prior export -- the signature of a run whose cumulative
    store was not restored, which is about to republish a truncated
    `latest/out/transactions.csv`. Surfacing it here makes the guard visible
    in a report rather than only in a run log."""
    path = config.ESPN_DIR / "last_export.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return data.get("reason") if data.get("stale") else None


def _espn_freshness(season=None):
    """Feed-level freshness for the report header: when did *any* ESPN view
    last land. See `espn_view_freshness` for the per-view question, which is
    what a correctness gate must ask instead.

    A shrunken last export also marks the feed stale: the data can be
    minutes old and still be missing most of itself."""
    season = season or config.SEASON
    season_dir = config.RAW_DIR / str(season)
    export_warning = espn_export_warning()
    if not season_dir.exists():
        return None, True
    fetched_ats = []
    for meta_path in season_dir.glob("*.meta.json"):
        try:
            fetched_ats.append(json.loads(meta_path.read_text()).get("fetched_at"))
        except (json.JSONDecodeError, OSError):
            continue
    fetched_ats = [f for f in fetched_ats if f is not None]
    if not fetched_ats:
        return None, True
    latest = max(fetched_ats)
    aged_out = (time.time() - latest) > ESPN_STALE_HOURS * 3600
    return latest, bool(aged_out or export_warning)


def _odds_freshness():
    """The `slate_context` job specifically -- not the whole last_run.json
    -- so one odds job's staleness never masks another's
    (docs/report-weekly-schedule.md:336-341). Prefers the team-totals
    parquet's own max `captured_at` (an ISO string written by
    odds/jobs.py's `_flatten_featured`, converted to epoch here) over the
    last-run file's `ran_at`, because a budget-aborted run
    (docs/odds-budget.md) writes a fresh `ran_at` over an unchanged
    snapshot -- `ran_at` says when the job ran, `captured_at` says when its
    contents were true, the same "never a file's mtime" principle this
    module's docstring states, applied one level in.

    `stale` is True when the last-run file or its `slate_context` entry is
    absent, when that entry's own `stale` flag is set, or when the parquet
    is missing or empty."""
    entry = odds_store.read_last_run("slate_context")
    totals_df = pd.read_parquet(config.ODDS_TEAM_TOTALS) if config.ODDS_TEAM_TOTALS.exists() else pd.DataFrame()

    captured_at = None
    if not totals_df.empty and "captured_at" in totals_df.columns:
        captured_at = pd.Timestamp(totals_df["captured_at"].max()).timestamp()

    ran_at = entry.get("ran_at") if entry else None
    stale = entry is None or bool(entry.get("stale")) or totals_df.empty
    return (captured_at if captured_at is not None else ran_at), stale


def freshness(season=None):
    """{"sleeper": (fetched_at, stale), "nflverse": (fetched_at, stale),
    "espn": (fetched_at, stale), "odds": (fetched_at, stale)} -- the
    freshness header every report opens with. `fetched_at` is a Unix
    timestamp or None when the feed has never run; `stale` is always a
    bool."""
    return {
        "sleeper": _sleeper_freshness(),
        "nflverse": _nflverse_freshness(),
        "espn": _espn_freshness(season=season),
        "odds": _odds_freshness(),
    }
