"""HTTP client for Sleeper's public read API.

Free, unauthenticated, no per-league scoping -- our league lives on ESPN, this
only pulls the NFL-wide player pool and trending lists. `espn_ff/client.py`
no longer holds a monopoly on the network; this is the second and last module
that touches it.
"""

import time

import requests

BASE = "https://api.sleeper.app"

RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4
BACKOFF_BASE = 1.5


class SleeperError(RuntimeError):
    """Any non-retryable failure talking to Sleeper."""


class SleeperClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0"})

    def _get(self, path, params=None):
        url = f"{BASE}{path}"
        last_error = None
        for attempt in range(MAX_ATTEMPTS):
            response = self.session.get(url, params=params, timeout=30)

            if response.status_code in RETRY_STATUS:
                last_error = f"HTTP {response.status_code} from {response.url}"
                if attempt < MAX_ATTEMPTS - 1:
                    time.sleep(BACKOFF_BASE ** attempt)
                    continue
                raise SleeperError(f"{last_error} after {MAX_ATTEMPTS} attempts")

            response.raise_for_status()
            return response.json()

        raise SleeperError(last_error or "request failed")

    def get_players(self):
        """Full NFL player pool, keyed by sleeper_id. ~15 MB, not paginated.

        Sleeper asks that this endpoint be called at most once a day --
        callers must go through espn_ff.sleeper.snapshots.fetch_players,
        which enforces that discipline. Never call this directly from
        anything request-shaped.
        """
        return self._get("/v1/players/nfl")

    def get_trending(self, kind, lookback_hours=24, limit=25):
        """Community add/drop trending. kind is 'add' or 'drop'."""
        if kind not in ("add", "drop"):
            raise ValueError(f"kind must be 'add' or 'drop', got {kind!r}")
        return self._get(
            f"/v1/players/nfl/trending/{kind}",
            params={"lookback_hours": lookback_hours, "limit": limit},
        )
