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
from ..weeks import ET

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


def features(season, week):
    """The (season, week) slice of data/nflverse/player_week_features.parquet.
    Empty frame -- not an exception -- when the file is absent, so a
    scheduled run degrades to `insufficient data` at exit 0. Read directly
    off `config.NFLVERSE_FEATURES` rather than through `latest_export`: the
    parquet is written straight to data/nflverse/ by `cmd_features`, not
    dated into data/out/ -- the CSV export there is a `--min-snap-pct`
    display filter, not the canonical table."""
    if not config.NFLVERSE_FEATURES.exists():
        return pd.DataFrame()
    df = pd.read_parquet(config.NFLVERSE_FEATURES)
    return df[(df["season"] == season) & (df["week"] == week)].reset_index(drop=True)


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


# The nflverse datasets `nflverse.features.build` actually reads -- not
# every dataset in the manifest (players/injuries/depth_charts feed other
# things, not player_week_features's provisional flag or its snap/target
# columns).
_FEATURES_DATASETS = ("schedules", "snap_counts", "stats_player")


def nflverse_features_freshness(season=None):
    """`fetched_at` for the specific nflverse datasets behind
    `player_week_features.parquet`, not the feed-level max `_nflverse_freshness`
    returns across every dataset (players, injuries, depth_charts included).
    Named in the canonical-read gate's reason string when the features table
    is absent or still provisional -- the `espn_view_freshness` precedent
    above, applied to nflverse: a narrow accessor per caller rather than a
    widened feed-level one. `season`-seasonal datasets (snap_counts,
    stats_player) are keyed `name/season` in the manifest; `schedules` is
    not seasonal and is keyed by bare name."""
    season = season or config.SEASON
    manifest = nflverse_store.read_manifest()
    fetched_ats = []
    for name in _FEATURES_DATASETS:
        entry = manifest.get(f"{name}/{season}") or manifest.get(name)
        if entry and entry.get("fetched_at") is not None:
            fetched_ats.append(entry["fetched_at"])
    return max(fetched_ats) if fetched_ats else None


def espn_view_freshness(views, season=None, scoring_period=None):
    """`fetched_at` for one specific set of ESPN views, or None when that
    view has never been fetched.

    `scoring_period` narrows further, to the one cached payload fetched for
    that week. It matters for `mRoster` and nothing else: every other view
    here is fetched league-wide with no `scoringPeriodId` and so has exactly
    one cache file, while mRoster has one per week. Without the narrowing,
    the `max()` below answers "when did any week's roster last land", so a
    `--refresh-weeks 1` refetch of a completed week would report the live
    week's roster as current. Matched on the sidecar's recorded `url` rather
    than by rebuilding the cache key, so this needs no league id and stays
    correct under a non-default `--league-id`.

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
            meta = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if scoring_period is not None:
            url = meta.get("url") or ""
            if f"scoringPeriodId={scoring_period}" not in url:
                continue
        fetched_ats.append(meta.get("fetched_at"))
    fetched_ats = [f for f in fetched_ats if f is not None]
    return max(fetched_ats) if fetched_ats else None


# The view tuple `cmd_export` fetches rosters with -- NOT `cmd_pull`'s
# ["mRoster", "mTeam", "mMatchupScore"], which is a different cache key
# entirely. weekly-rosters.csv is built from the export's fetch, so this is
# the payload whose age actually answers "is the roster below current".
_ROSTER_VIEWS = ["mRoster", "mTeam"]

# How old the roster payload may be before a report stops calling it
# current. **Inferred, not measured**: `cmd_report` refreshes mRoster
# in-process immediately before building, so a healthy run's payload is
# seconds old, and an hour is slack for a slow runner rather than a
# tolerance anyone observed. Anything older means this run's own refresh did
# not land.
ROSTER_MAX_AGE_SECONDS = 3600


def roster_read_is_current(rendered_at, season=None, week=None):
    """Whether the `mRoster` payload behind `weekly-rosters.csv` was fetched
    by (or just before) the run rendering at `rendered_at`.

    `week` is the scoring period whose roster the report actually displays.
    Pass it: mRoster is cached per week, so without it this answers "did any
    week's roster land recently", which a refetch of a completed week can
    satisfy while the displayed week's roster is a day old.

    Returns (current: bool, reason: str|None) -- the same shape
    `waivers.waiver_read_is_settled` returns, and for the same reason: the
    question is not "is this data recent" in the abstract but "was it
    fetched for this render".

    Deliberately NOT `_espn_freshness`. That takes `max(fetched_at)` across
    every cached view against a flat 24-hour threshold, so any recent fetch
    of any view reports the roster as fresh. On 2026-09-17 the Thursday
    report printed `espn: 2026-09-16 11:02 ET` unflagged -- inside the
    24-hour window, and masking an `mRoster` payload that predated a
    Wednesday-afternoon trade -- and listed the traded-away player as
    rostered (Observed). `espn_view_freshness` asks the narrow question;
    this wraps it in the boundary that makes it a gate.

    The filename date on `weekly-rosters.csv` cannot substitute:
    `scripts/s3_sync.sh`'s `cmd_restore_out` re-stamps every restored export
    with today's date, so `latest_export` always sees a file that looks
    like today's.
    """
    fetched_at = espn_view_freshness(_ROSTER_VIEWS, season=season, scoring_period=week)
    if fetched_at is None:
        return False, "the ESPN roster payload has never been fetched"
    age = rendered_at - fetched_at
    if age > ROSTER_MAX_AGE_SECONDS:
        fetched = datetime.fromtimestamp(fetched_at, ET)
        return False, (
            f"the ESPN roster payload was fetched {fetched:%a %Y-%m-%d %H:%M ET}, "
            f"{age / 3600:.1f} hours before this render -- any roster, trade or "
            "lineup move since then is invisible here"
        )
    return True, None


def roster_staleness_note(rendered_at, season=None, week=None):
    """The "what this report cannot see" note for a roster that was not
    re-pulled for this render, or None when it was. One accessor rather than
    the (bool, reason) pair so each report builder appends one line, matching
    how `espn_export_warning` is consumed.

    The wording claims only what the threshold actually establishes. An
    earlier draft opened "The roster above is not this morning's", which is
    false in the very case this fires most often: the in-process refresh dies
    at 11:00 but `espn-daily` landed at 09:08, so the payload is 1.9 hours
    old -- past the bound, and still unambiguously this morning's. A gate
    that prints a false claim is worse than no gate. `reason` carries the
    absolute timestamp and the age, which is what lets a reader tell two
    hours from twenty-six; do not add a second threshold to say it for them.
    """
    current, reason = roster_read_is_current(rendered_at, season=season, week=week)
    if current:
        return None
    return f"The roster tables above were not re-pulled for this render -- {reason}."


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
