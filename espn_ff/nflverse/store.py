"""On-disk store for nflverse release assets: layout, manifest, freshness.

Modelled on espn_ff/sleeper/snapshots.py, adapted for the shape of the data:
each asset here is one whole-season file that is rebuilt in place several
times a day, not a daily snapshot to retain -- so there is no season
partitioning and no retention problem to solve. Downloads go through
NflverseClient.download, which already handles the `.partial` + os.replace
swap; refresh() adds the layer sleeper/snapshots.fetch_players plays for
Sleeper -- deciding whether a request is worth making at all, and never
letting a single dead feed abort the run.
"""

import json
import time
from dataclasses import dataclass

import pandas as pd

from .. import config
from .client import NflverseClient, NflverseError
from .datasets import DATASETS, assert_schema


@dataclass
class RefreshResult:
    name: str
    season: object
    status: str  # "updated" | "unchanged" | "missing" | "error"
    rows: object = None
    message: str = None
    last_updated: object = None
    etag: object = None


def _manifest_key(name, season=None):
    return f"{name}/{season}" if season is not None else name


def local_path(name, season=None):
    dataset = DATASETS[name]
    return config.NFLVERSE_RAW_DIR / name / dataset.resolved_filename(season)


def read_manifest():
    if config.NFLVERSE_MANIFEST.exists():
        return json.loads(config.NFLVERSE_MANIFEST.read_text())
    return {}


def write_manifest(manifest):
    config.NFLVERSE_DIR.mkdir(parents=True, exist_ok=True)
    config.NFLVERSE_MANIFEST.write_text(json.dumps(manifest, indent=2, default=str))


def is_stale(entry, hours=36):
    """True when `entry` is missing, or its last successful check is older
    than `hours`. The brief's staleness banner -- checked, never enforced."""
    if not entry or entry.get("fetched_at") is None:
        return True
    return (time.time() - entry["fetched_at"]) > hours * 3600


def _read_parquet(path):
    return pd.read_parquet(path)


def _refresh_one(client, name, dataset, season, prior_entry, force):
    prior_entry = prior_entry or {}

    try:
        remote_ts = client.get_timestamp(dataset.tag)
    except Exception as exc:  # noqa: BLE001 -- a single dead feed must not abort the run
        return RefreshResult(name, season, "error", message=str(exc))

    if remote_ts is None:
        return RefreshResult(name, season, "missing", message="no timestamp.json for this feed")

    dest = local_path(name, season)
    if not force and remote_ts == prior_entry.get("last_updated") and dest.exists():
        return RefreshResult(
            name, season, "unchanged", rows=prior_entry.get("rows"),
            last_updated=remote_ts, etag=prior_entry.get("etag"),
        )

    etag = None if force else prior_entry.get("etag")
    try:
        path, new_etag, status = client.download(dataset.tag, dataset.resolved_filename(season), dest, etag=etag)
    except Exception as exc:  # noqa: BLE001
        return RefreshResult(name, season, "error", message=str(exc))

    if status == "missing":
        return RefreshResult(name, season, "missing", message="404 on release asset")
    if status == "unchanged":
        return RefreshResult(
            name, season, "unchanged", rows=prior_entry.get("rows"),
            last_updated=remote_ts, etag=new_etag or prior_entry.get("etag"),
        )

    try:
        df = _read_parquet(path)
        assert_schema(name, df.columns)
    except Exception as exc:  # noqa: BLE001 -- a bad download must not wedge the run
        return RefreshResult(name, season, "error", message=str(exc))

    return RefreshResult(name, season, "updated", rows=len(df), last_updated=remote_ts, etag=new_etag)


def refresh(names=None, seasons=None, client=None, force=False):
    """Refresh each (dataset, season) pair. Every step is wrapped so a
    single dead feed degrades that one entry to status="missing"/"error"
    and never aborts the run -- callers decide what to do about an optional
    vs. required dataset failing; this function just reports."""
    client = client or NflverseClient()
    names = list(names) if names else list(DATASETS)
    manifest = read_manifest()
    results = []

    for name in names:
        dataset = DATASETS[name]
        seasons_for = seasons if dataset.seasonal else [None]
        for season in seasons_for:
            key = _manifest_key(name, season)
            result = _refresh_one(client, name, dataset, season, manifest.get(key), force)
            entry = dict(manifest.get(key) or {})
            if result.status in ("updated", "unchanged"):
                entry = {
                    "last_updated": result.last_updated,
                    "etag": result.etag,
                    "fetched_at": time.time(),
                    "rows": result.rows,
                    "status": result.status,
                }
            else:
                entry["status"] = result.status
                entry["fetched_at"] = time.time()
                if result.message:
                    entry["message"] = result.message
            manifest[key] = entry
            results.append(result)

    write_manifest(manifest)
    return results


def load(name, season=None):
    """Read a dataset off disk into a schema-asserted DataFrame.

    A required dataset with nothing on disk raises -- there is no sane
    empty-table default for players/snap_counts/stats_player/schedules. An
    optional dataset (injuries, depth_charts) logs a warning and returns an
    empty frame instead, matching the "die gracefully" contract for feeds
    that have gone dark before.
    """
    dataset = DATASETS[name]
    path = local_path(name, season)

    if not path.exists():
        if dataset.optional:
            print(f"  [warning] {name}: no data on disk, returning empty frame")
            return pd.DataFrame(columns=sorted(dataset.required))
        raise NflverseError(f"{name}: no data on disk at {path} -- run `nflverse` first")

    df = _read_parquet(path)
    assert_schema(name, df.columns)

    if name == "depth_charts" and not df.empty:
        latest_dt = df["dt"].max()
        df = df[df["dt"] == latest_dt].reset_index(drop=True)

    return df
