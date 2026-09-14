"""espn_ff.odds.client -- fixtures only, no network, no real credits.
Covers handoff §11 acceptance criteria one-for-one via a fake requests
session that records every call, so a test can assert one was never made."""

import datetime as dt
import json
from pathlib import Path

import pytest
import requests

from espn_ff.odds import client as client_module
from espn_ff.odds import ledger, markets, store

FIXTURES = Path(__file__).parent / "fixtures" / "odds"


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None, url="https://api.the-odds-api.com/v4/x"):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = json.dumps(json_data) if json_data is not None else ""
        self.url = url

    def json(self):
        return json.loads(self.text)


class _FakeSession:
    """Records every call() so a test can assert an HTTP call was never
    made -- the point of the guard invariant. `responses` is a queue: each
    .get() pops the next entry, which may be an exception to raise."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if not self.responses:
            raise AssertionError("fake session ran out of queued responses")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _headers(last=2, used=2, remaining=398):
    return {"x-requests-last": str(last), "x-requests-used": str(used), "x-requests-remaining": str(remaining)}


@pytest.fixture
def odds_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(store.config, "ODDS_DIR", tmp_path)
    monkeypatch.setattr(store.config, "ODDS_RAW_DIR", tmp_path / "raw")
    monkeypatch.delenv("ODDS_QUOTA_RESET_DAY", raising=False)
    return tmp_path


@pytest.fixture
def con(tmp_path):
    return ledger.open_db(tmp_path / "ledger.db")


@pytest.fixture
def job_run_id(con):
    return ledger.start_run(con, "props_primary", run_budget=40)


def _client(con, job_run_id, responses, **kwargs):
    c = client_module.OddsClient(api_key="test-secret-key", con=con, job_run_id=job_run_id, **kwargs)
    c.session = _FakeSession(responses)
    return c


def test_regions_with_a_comma_throws_at_construction(con, job_run_id):
    with pytest.raises(ledger.OddsError):
        client_module.OddsClient(api_key="k", con=con, job_run_id=job_run_id, regions="us,uk")


def test_more_than_ten_bookmakers_throws_at_construction(con, job_run_id):
    with pytest.raises(ledger.OddsError):
        client_module.OddsClient(
            api_key="k", con=con, job_run_id=job_run_id, bookmakers=tuple(f"book{i}" for i in range(11))
        )


def test_any_historical_url_throws(con, job_run_id, odds_paths):
    c = _client(con, job_run_id, responses=[])
    with pytest.raises(ledger.OddsError):
        c._build_url("/historical/sports/{sport}/odds")


def test_ledger_stubbed_to_raise_means_zero_http_calls(con, job_run_id, odds_paths, monkeypatch):
    monkeypatch.setattr(ledger, "guard", lambda *a, **kw: (_ for _ in ()).throw(ledger.OddsError("no")))
    c = _client(con, job_run_id, responses=[_FakeResponse(200, {}, headers=_headers())])
    with pytest.raises(ledger.OddsError):
        c.get_featured_odds(["spreads", "totals"])
    assert c.session.calls == []


def test_free_endpoints_never_touch_the_ledger(con, job_run_id, odds_paths):
    events = json.loads((FIXTURES / "events.json").read_text())
    c = _client(con, job_run_id, responses=[_FakeResponse(200, events), _FakeResponse(200, [{"key": "americanfootball_nfl", "active": True}])])

    result = c.get_events()
    assert result == events
    c.get_sports()

    count = con.execute("SELECT COUNT(*) AS n FROM credit_ledger_entry").fetchone()["n"]
    assert count == 0


def test_get_featured_odds_returns_parsed_json_and_reconciles(con, job_run_id, odds_paths):
    payload = json.loads((FIXTURES / "featured_odds.json").read_text())
    c = _client(con, job_run_id, responses=[_FakeResponse(200, payload, headers=_headers(last=2, used=2, remaining=398))])

    result = c.get_featured_odds(["spreads", "totals"])
    assert result == payload

    row = con.execute("SELECT * FROM credit_ledger_entry").fetchone()
    assert row["status"] == "reconciled"
    assert row["actual_cost"] == 2
    assert row["endpoint"] == "featured_odds"


def test_a_429_retry_creates_a_second_ledger_entry_not_a_reused_one(con, job_run_id, odds_paths, monkeypatch):
    monkeypatch.setattr(client_module.time, "sleep", lambda s: None)
    payload = json.loads((FIXTURES / "event_odds.json").read_text())
    c = _client(
        con, job_run_id,
        responses=[_FakeResponse(429, headers=_headers(last=0, used=0, remaining=400)), _FakeResponse(200, payload, headers=_headers())],
    )

    result = c.get_event_odds("evt_gb_min_2026wk3", ["player_receptions", "player_anytime_td"])
    assert result == payload

    entries = con.execute("SELECT id, status FROM credit_ledger_entry ORDER BY id").fetchall()
    assert len(entries) == 2
    assert len(c.session.calls) == 2


def test_timeout_with_no_response_records_failed_assumed_charged_at_estimated_cost(con, job_run_id, odds_paths, monkeypatch):
    monkeypatch.setattr(client_module.time, "sleep", lambda s: None)
    c = _client(
        con, job_run_id,
        responses=[requests.exceptions.Timeout("boom")] * client_module.MAX_ATTEMPTS,
    )

    with pytest.raises(ledger.OddsError):
        c.get_featured_odds(["spreads", "totals"])

    entries = con.execute("SELECT status, actual_cost, estimated_cost FROM credit_ledger_entry").fetchall()
    assert len(entries) == client_module.MAX_ATTEMPTS
    for entry in entries:
        assert entry["status"] == "failed_assumed_charged"
        assert entry["actual_cost"] == entry["estimated_cost"]


def test_raw_response_is_persisted_before_parsing(con, job_run_id, odds_paths, monkeypatch):
    payload = json.loads((FIXTURES / "featured_odds.json").read_text())
    fixed_clock = lambda: dt.datetime(2026, 9, 18, 12, 0, 0, tzinfo=dt.timezone.utc)
    c = _client(con, job_run_id, responses=[_FakeResponse(200, payload, headers=_headers())], clock=fixed_clock)

    monkeypatch.setattr(client_module.json, "loads", lambda *a, **kw: (_ for _ in ()).throw(ValueError("stubbed parse failure")))

    with pytest.raises(ledger.OddsError):
        c.get_featured_odds(["spreads", "totals"])

    period = ledger.current_billing_period()
    ts = fixed_clock().strftime("%Y%m%dT%H%M%S%fZ")
    dest = store.raw_path(period, job_run_id, "featured_odds", None, ts)
    assert dest.exists()
    assert dest.read_text() == json.dumps(payload)


def test_api_key_is_redacted_from_every_error_message(con, job_run_id, odds_paths, monkeypatch):
    monkeypatch.setattr(client_module.time, "sleep", lambda s: None)
    c = _client(con, job_run_id, responses=[_FakeResponse(500, {"error": "boom"}, url="https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds?apiKey=test-secret-key")] * client_module.MAX_ATTEMPTS)

    with pytest.raises(ledger.OddsError) as exc_info:
        c.get_featured_odds(["spreads"])

    assert "test-secret-key" not in str(exc_info.value)
    assert "***" in str(exc_info.value)
