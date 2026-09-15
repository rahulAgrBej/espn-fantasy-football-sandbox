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


def _espn_freshness(season=None):
    season = season or config.SEASON
    season_dir = config.RAW_DIR / str(season)
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
    return latest, (time.time() - latest) > ESPN_STALE_HOURS * 3600


def freshness(season=None):
    """{"sleeper": (fetched_at, stale), "nflverse": (fetched_at, stale),
    "espn": (fetched_at, stale)} -- the freshness header every report
    opens with. `fetched_at` is a Unix timestamp or None when the feed has
    never run; `stale` is always a bool."""
    return {
        "sleeper": _sleeper_freshness(),
        "nflverse": _nflverse_freshness(),
        "espn": _espn_freshness(season=season),
    }
