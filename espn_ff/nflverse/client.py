"""HTTP client for nflverse release assets on GitHub.

Free, public, unauthenticated. Release assets are periodically rebuilt
parquet files, not a request-shaped API: freshness is checked via each
release tag's `timestamp.json` and a conditional GET, not a cache TTL.
`espn_ff/client.py` talks to ESPN and `espn_ff/sleeper/client.py` talks to
Sleeper; this is the third and last module that touches the network.

Verified live: GitHub redirects a release-asset GET (302) to a signed Azure
blob URL, and `If-None-Match` sent on the original request is honoured
through that redirect -- a stored ETag reliably gets a 304 with zero body
bytes, so the download-avoidance design here is not speculative.
"""

import os
import time

import requests

BASE = "https://github.com/nflverse/nflverse-data/releases/download"

RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4
BACKOFF_BASE = 1.5


class NflverseError(RuntimeError):
    """Any non-retryable failure talking to nflverse."""


class NflverseClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0"})

    def _get(self, url, headers=None):
        last_error = None
        for attempt in range(MAX_ATTEMPTS):
            response = self.session.get(url, headers=headers, timeout=30)

            if response.status_code in RETRY_STATUS:
                last_error = f"HTTP {response.status_code} from {response.url}"
                if attempt < MAX_ATTEMPTS - 1:
                    time.sleep(BACKOFF_BASE ** attempt)
                    continue
                raise NflverseError(f"{last_error} after {MAX_ATTEMPTS} attempts")

            return response

        raise NflverseError(last_error or "request failed")

    def get_timestamp(self, tag):
        """Release-asset build time, e.g. "2026-09-14 08:17:25 EDT", or None
        if the tag has no timestamp.json -- a dead feed, not an error."""
        response = self._get(f"{BASE}/{tag}/timestamp.json")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json().get("last_updated")

    def download(self, tag, filename, dest, etag=None):
        """GET a release asset to `dest`.

        Writes to a `.partial` sibling and os.replace()s it into place only
        on success, so a killed run never leaves a truncated parquet where a
        valid one used to be. Returns (path, new_etag, status) with status
        one of:

          "updated"    -- downloaded, dest now holds the new bytes
          "unchanged"  -- 304 against the given etag, dest untouched
          "missing"    -- 404; every loader must degrade rather than raise
        """
        headers = {"If-None-Match": etag} if etag else None
        response = self._get(f"{BASE}/{tag}/{filename}", headers=headers)

        if response.status_code == 304:
            return dest, etag, "unchanged"
        if response.status_code == 404:
            return None, None, "missing"
        response.raise_for_status()

        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_name(dest.name + ".partial")
        partial.write_bytes(response.content)
        os.replace(partial, dest)
        return dest, response.headers.get("ETag"), "updated"

    def download_text(self, url):
        """Plain GET for a non-release asset (e.g. dynastyprocess's
        db_playerids.csv, which lives on raw.githubusercontent.com, not a
        GitHub release). No ETag discipline -- this is a small file fetched
        as a best-effort fallback, not a daily-cadence asset."""
        response = self._get(url)
        response.raise_for_status()
        return response.text
