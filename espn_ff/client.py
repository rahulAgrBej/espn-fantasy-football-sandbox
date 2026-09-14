"""HTTP client for ESPN's undocumented fantasy API.

Every response is written to the on-disk cache before it is returned.
`espn_ff/sleeper/client.py` also touches the network, for the player-status
layer -- this is the ESPN half of it, not the only module that does.
"""

import json
import time

import requests

from . import cache, config


class EspnError(RuntimeError):
    """Any non-retryable failure talking to ESPN."""


class PrivateLeagueError(EspnError):
    """League requires session cookies we do not have."""


RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4
BACKOFF_BASE = 1.5


class EspnClient:
    def __init__(self, season=config.SEASON, league_id=config.LEAGUE_ID, refresh=False):
        self.season = season
        self.league_id = league_id
        self.refresh = refresh
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": config.USER_AGENT})
        self.session.cookies.update(config.cookies())

    # ---- low-level -------------------------------------------------------

    def _request(self, url, params, filter_header=None):
        headers = {}
        if filter_header:
            headers["X-Fantasy-Filter"] = json.dumps(filter_header)

        last_error = None
        for attempt in range(MAX_ATTEMPTS):
            response = self.session.get(url, params=params, headers=headers, timeout=30)

            if response.status_code == 401:
                raise PrivateLeagueError(
                    f"401 from {response.url}\n"
                    "This league is private. Set ESPN_S2 and SWID (see .env.example) "
                    "from a logged-in browser session and retry."
                )
            if response.status_code == 404:
                raise EspnError(
                    f"404 from {response.url}\n"
                    "No such league/season. For seasons before 2018 use the "
                    "leagueHistory endpoint instead."
                )
            if response.status_code in RETRY_STATUS:
                last_error = f"HTTP {response.status_code} from {response.url}"
                if attempt < MAX_ATTEMPTS - 1:
                    time.sleep(BACKOFF_BASE ** attempt)
                    continue
                raise EspnError(f"{last_error} after {MAX_ATTEMPTS} attempts")

            response.raise_for_status()
            return response.json(), response.url

        raise EspnError(last_error or "request failed")

    def _fetch(self, url, views, params, filter_header=None, ttl=None):
        """Cache-aware GET. Returns (data, served_from_cache)."""
        key = cache.cache_key(self.season, self.league_id, views, params, filter_header)
        path = cache.cache_path(self.season, views, key)

        if not self.refresh:
            cached = cache.read(path, ttl=ttl)
            if cached is not None:
                return cached, True

        # `view` repeats, so params are passed as a list of pairs.
        query = [("view", v) for v in views] + [
            (k, v) for k, v in dict(params or {}).items() if v is not None
        ]
        data, resolved_url = self._request(url, query, filter_header)
        cache.write(path, data, url=resolved_url)
        return data, False

    # ---- endpoints -------------------------------------------------------

    @property
    def league_url(self):
        return f"{config.BASE}/seasons/{self.season}/segments/0/leagues/{self.league_id}"

    def get_league(self, views, scoring_period=None, ttl=None, **params):
        """Fetch one or more views against the league endpoint.

        Multiple views in a single call return a richer joined payload than the
        same views fetched separately.
        """
        views = [views] if isinstance(views, str) else list(views)
        if scoring_period is not None:
            params["scoringPeriodId"] = scoring_period
        data, _ = self._fetch(self.league_url, views, params, ttl=ttl)
        return data

    def get_player_pool(self, limit=2000, filter_header=None):
        """Public player pool. Needs no auth; paging is header-controlled."""
        url = f"{config.BASE}/seasons/{self.season}/segments/0/leaguedefaults/{config.PLAYER_POOL_ID}"
        filter_header = filter_header or {
            "players": {
                "limit": limit,
                "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
            }
        }
        data, _ = self._fetch(url, ["kona_player_info"], {}, filter_header=filter_header)
        return data

    def get_platform_settings(self):
        """Season-level metadata: the 235-entry stat dictionary and lookups."""
        url = f"{config.BASE}/seasons/{self.season}"
        data, _ = self._fetch(url, ["chui_default_platformsettings"], {})
        return data[0] if isinstance(data, list) else data

    def current_scoring_period(self):
        return self.get_platform_settings().get("currentScoringPeriod", {}).get("id")
