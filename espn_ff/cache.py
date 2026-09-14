"""On-disk cache for raw ESPN JSON responses.

Fantasy data is immutable once a scoring period closes, so completed weeks are
cached forever and only the live week is re-fetched. Every entry gets a sidecar
recording the request that produced it.
"""

import hashlib
import json
import re
import time
from pathlib import Path

from . import config

# Seconds a payload covering the in-progress week stays fresh.
LIVE_TTL = 300


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:60] or "payload"


def cache_key(season, league_id, views, params, filter_header=None):
    """Stable hash over everything that distinguishes one request from another."""
    payload = json.dumps(
        {
            "season": season,
            "league_id": league_id,
            "views": sorted(views),
            "params": sorted((str(k), str(v)) for k, v in dict(params or {}).items()),
            "filter": filter_header,
        },
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


def cache_path(season, views, key):
    return config.RAW_DIR / str(season) / f"{_slug('-'.join(sorted(views)))}-{key}.json"


def _meta_path(path):
    return path.with_suffix(".meta.json")


def read(path, ttl=None):
    """Return cached JSON, or None when absent or stale."""
    path = Path(path)
    if not path.exists():
        return None
    if ttl is not None:
        meta = read_meta(path)
        fetched = (meta or {}).get("fetched_at", 0)
        if time.time() - fetched > ttl:
            return None
    return json.loads(path.read_text())


def read_meta(path):
    meta = _meta_path(Path(path))
    return json.loads(meta.read_text()) if meta.exists() else None


def write(path, data, url=None, from_cache=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    _meta_path(path).write_text(
        json.dumps({"url": url, "fetched_at": time.time(), "from_cache": from_cache}, indent=2)
    )
    return path


def ttl_for(scoring_period, current_period):
    """Completed weeks never expire; the live week gets a short TTL."""
    if scoring_period is None or current_period is None:
        return LIVE_TTL
    return None if scoring_period < current_period else LIVE_TTL
