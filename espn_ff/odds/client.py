"""HTTP client for The Odds API. This is the fourth and last module in this
repo that touches the network -- espn_ff/client.py talks to ESPN,
espn_ff/sleeper/client.py to Sleeper, espn_ff/nflverse/client.py to
nflverse's GitHub release assets, and this one to a metered vendor.

Every other client in this repo can be re-run for free when in doubt. This
one cannot: a request here has a real dollar-equivalent cost, so every
paid call is routed through espn_ff.odds.ledger.guard/reconcile with no way
around it, and retries are deliberately expensive -- MAX_ATTEMPTS=3,
BACKOFF_BASE=2.0, MIN_REQUEST_INTERVAL=1.0s, diverging from the shared
4 / 1.5 the other three clients use, because a retry here is a new billed
request rather than a free do-over.
"""

import datetime as dt
import json
import time

import requests

from .. import config
from . import ledger, store
from .ledger import OddsError
from .markets import BOOKMAKERS, REGIONS, SPORT

BASE = "https://api.the-odds-api.com/v4"

RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3
BACKOFF_BASE = 2.0
MIN_REQUEST_INTERVAL = 1.0


def _redact(text, api_key):
    """Applied at the single place a URL, error, or exception message is
    stringified -- the one enforcement point for "never log the key"."""
    if not text or not api_key:
        return text
    return text.replace(api_key, "***")


class OddsClient:
    def __init__(self, api_key=None, con=None, job_run_id=None, clock=None,
                 regions=REGIONS, bookmakers=BOOKMAKERS):
        if "," in regions:
            raise OddsError(f"regions must be a single region (got a comma-joined value: {regions!r})")
        if len(bookmakers) > 10:
            raise OddsError(f"at most 10 bookmakers allowed, got {len(bookmakers)}")

        self.api_key = api_key or config.odds_api_key()
        self.con = con or ledger.open_db()
        self.job_run_id = job_run_id
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.regions = regions
        self.bookmakers = bookmakers
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": config.USER_AGENT})
        self._last_request_at = None

    def __repr__(self):
        return _redact(f"OddsClient(regions={self.regions!r}, api_key={self.api_key!r})", self.api_key)

    def _build_url(self, path_template, **path_params):
        path = path_template.format(sport=SPORT, **path_params)
        url = f"{BASE}{path}"
        if "/historical/" in url:
            raise OddsError("historical endpoints are blocked -- paid tier only, never called from this client")
        return url

    def _wait_for_min_interval(self):
        if self._last_request_at is None:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - elapsed)

    def _get(self, url, params):
        self._wait_for_min_interval()
        try:
            response = self.session.get(url, params=params, timeout=30)
        finally:
            self._last_request_at = time.monotonic()
        return response

    # ---- free endpoints ---------------------------------------------

    def get_sports(self):
        """Free. Also the startup check for whether SPORT is still a live
        key, rather than trusting the constant unconditionally."""
        url = f"{BASE}/sports"
        response = self._get(url, params={"apiKey": self.api_key})
        if response.status_code >= 400:
            raise OddsError(_redact(f"HTTP {response.status_code} from {response.url}", self.api_key))
        return response.json()

    def assert_sport_live(self):
        sports = self.get_sports()
        if not any(s.get("key") == SPORT and s.get("active") for s in sports):
            raise OddsError(f"{SPORT!r} is not an active sport key per /v4/sports -- check upstream before continuing")

    def get_events(self):
        """Free."""
        url = self._build_url("/sports/{sport}/events")
        response = self._get(url, params={"apiKey": self.api_key})
        if response.status_code >= 400:
            raise OddsError(_redact(f"HTTP {response.status_code} from {response.url}", self.api_key))
        return response.json()

    # ---- guarded (metered) endpoints ---------------------------------

    def get_featured_odds(self, markets, priority="normal"):
        return self._guarded_get(
            endpoint="featured_odds",
            path_template="/sports/{sport}/odds",
            params={
                "apiKey": self.api_key,
                "regions": self.regions,
                "markets": ",".join(markets),
                "bookmakers": ",".join(self.bookmakers),
                "oddsFormat": "american",
                "dateFormat": "iso",
            },
            priority=priority,
            markets_requested=list(markets),
        )

    def get_event_odds(self, event_id, markets, priority="normal"):
        return self._guarded_get(
            endpoint="event_odds",
            path_template="/sports/{sport}/events/{event_id}/odds",
            path_params={"event_id": event_id},
            params={
                "apiKey": self.api_key,
                "regions": self.regions,
                "markets": ",".join(markets),
                "bookmakers": ",".join(self.bookmakers),
                "oddsFormat": "american",
                "dateFormat": "iso",
            },
            priority=priority,
            event_id=event_id,
            markets_requested=list(markets),
        )

    def get_scores(self, days_from=None, priority="normal"):
        params = {"apiKey": self.api_key, "dateFormat": "iso"}
        if days_from is not None:
            params["daysFrom"] = days_from
        # Cost is 1, or 2 with daysFrom -- not markets-shaped, so the
        # estimate is expressed as a one-element "markets_requested" so the
        # guard's generic `len(markets_requested)` estimator still applies.
        return self._guarded_get(
            endpoint="scores",
            path_template="/sports/{sport}/scores",
            params=params,
            priority=priority,
            markets_requested=["scores"] if days_from is None else ["scores", "daysFrom"],
        )

    # ---- the guarded request path, with no way around it ------------

    def _guarded_get(self, endpoint, path_template, params, priority, event_id=None,
                      markets_requested=None, path_params=None):
        path_params = path_params or {}
        markets_requested = markets_requested or []
        estimated_cost = max(len(markets_requested), 1)
        last_error = None

        for attempt in range(MAX_ATTEMPTS):
            # Step 1+2: estimate, then guard -- one ledger entry per attempt,
            # because a retry here is a new billed request.
            entry_id = ledger.guard(
                self.con,
                {
                    "endpoint": endpoint,
                    "event_id": event_id,
                    "markets_requested": markets_requested,
                    "regions": self.regions,
                    "estimated_cost": estimated_cost,
                },
                priority=priority,
                job_run_id=self.job_run_id,
            )

            response, exc = None, None
            try:
                url = self._build_url(path_template, **path_params)
                response = self._get(url, params=params)
            except requests.RequestException as e:
                exc = e

            if response is not None:
                # Step 5: persist raw text before parsing, unconditionally.
                billing_period = ledger.current_billing_period()
                store.archive_raw(
                    response.text, billing_period, self.job_run_id, endpoint, event_id,
                    ts=self.clock().strftime("%Y%m%dT%H%M%S%fZ"),
                )

            # Steps 6/7: reconcile from headers, or assume charged on failure.
            ledger.reconcile(self.con, entry_id, response=response, exc=exc)

            if exc is not None:
                last_error = _redact(str(exc), self.api_key)
                if attempt < MAX_ATTEMPTS - 1:
                    time.sleep(BACKOFF_BASE**attempt)
                    continue
                raise OddsError(f"{last_error} after {MAX_ATTEMPTS} attempts")

            if response.status_code in RETRY_STATUS:
                last_error = _redact(f"HTTP {response.status_code} from {response.url}", self.api_key)
                if attempt < MAX_ATTEMPTS - 1:
                    time.sleep(BACKOFF_BASE**attempt)
                    continue
                raise OddsError(f"{last_error} after {MAX_ATTEMPTS} attempts")

            if response.status_code >= 400:
                raise OddsError(
                    _redact(f"HTTP {response.status_code} from {response.url}: {response.text[:200]}", self.api_key)
                )

            try:
                return json.loads(response.text)
            except ValueError as e:
                raise OddsError(f"{endpoint}: response was not valid JSON: {e}") from e

        raise OddsError(last_error or "request failed")
