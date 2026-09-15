"""Cache keying and the completed-week-vs-live-week freshness rule."""

import time

import pytest

from espn_ff import cache, config
from espn_ff import client as client_module
from espn_ff.client import EspnClient


def test_key_is_stable_across_view_order_and_value_type():
    a = cache.cache_key(2026, 1, ["mTeam", "mRoster"], {"scoringPeriodId": 1})
    b = cache.cache_key(2026, 1, ["mRoster", "mTeam"], {"scoringPeriodId": "1"})
    assert a == b


@pytest.mark.parametrize(
    "left,right",
    [
        ((2026, 1, ["mTeam"], {}), (2025, 1, ["mTeam"], {})),
        ((2026, 1, ["mTeam"], {}), (2026, 2, ["mTeam"], {})),
        ((2026, 1, ["mTeam"], {}), (2026, 1, ["mRoster"], {})),
        ((2026, 1, ["mTeam"], {"scoringPeriodId": 1}), (2026, 1, ["mTeam"], {"scoringPeriodId": 2})),
    ],
)
def test_key_separates_distinct_requests(left, right):
    assert cache.cache_key(*left) != cache.cache_key(*right)


def test_filter_header_participates_in_the_key():
    base = (2026, 1, ["kona_player_info"], {})
    assert cache.cache_key(*base, {"players": {"limit": 50}}) != cache.cache_key(
        *base, {"players": {"limit": 2000}}
    )


def test_completed_weeks_never_expire_live_week_does():
    assert cache.ttl_for(scoring_period=1, current_period=5) is None
    assert cache.ttl_for(scoring_period=5, current_period=5) == cache.LIVE_TTL
    assert cache.ttl_for(scoring_period=None, current_period=5) == cache.LIVE_TTL


def test_roundtrip_writes_a_sidecar(tmp_path):
    path = tmp_path / "payload.json"
    cache.write(path, {"hello": "world"}, url="https://example.test")
    assert cache.read(path) == {"hello": "world"}
    meta = cache.read_meta(path)
    assert meta["url"] == "https://example.test"
    assert meta["fetched_at"] <= time.time()


def test_read_honours_ttl(tmp_path):
    path = tmp_path / "payload.json"
    cache.write(path, {"a": 1})
    assert cache.read(path, ttl=60) == {"a": 1}
    assert cache.read(path, ttl=0) is None


def test_missing_file_reads_as_none(tmp_path):
    assert cache.read(tmp_path / "nope.json") is None
    assert cache.read_meta(tmp_path / "nope.json") is None


# ---- resolve_scoring_period: pure calendar resolution ---------------------

PERIOD_MS = 7 * 24 * 3600 * 1000  # one week, matching ESPN's real cadence
WEEK1_START = 1_700_000_000_000  # arbitrary epoch ms


def _periods(week1_start=WEEK1_START, week2_end=None):
    week2_end = week2_end if week2_end is not None else week1_start + 2 * PERIOD_MS
    return [
        {"id": 0, "startDate": 0, "endDate": 0},
        {"id": 1, "startDate": week1_start, "endDate": week1_start + PERIOD_MS},
        {"id": 2, "startDate": week1_start + PERIOD_MS, "endDate": week2_end},
    ]


def test_resolve_scoring_period_picks_the_containing_window():
    periods = _periods()
    assert client_module.resolve_scoring_period(periods, WEEK1_START + 1000) == 1
    assert client_module.resolve_scoring_period(periods, WEEK1_START + PERIOD_MS + 1000) == 2


def test_resolve_scoring_period_boundary_goes_to_the_later_period():
    periods = _periods()
    boundary = WEEK1_START + PERIOD_MS
    assert client_module.resolve_scoring_period(periods, boundary) == 2


def test_resolve_scoring_period_never_matches_the_preseason_sentinel():
    periods = _periods()
    assert client_module.resolve_scoring_period(periods, 0) is None


def test_resolve_scoring_period_past_the_final_period_resolves_to_none():
    periods = _periods()
    past_the_end = periods[-1]["endDate"] + 1
    assert client_module.resolve_scoring_period(periods, past_the_end) is None


# ---- EspnClient.current_scoring_period: cache-driven resolution -----------


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, url="https://example.test"):
        self.status_code = status_code
        self._json_data = json_data
        self.url = url

    def raise_for_status(self):
        pass

    def json(self):
        return self._json_data


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        if not self.responses:
            raise AssertionError("fake session ran out of queued responses")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RAW_DIR", tmp_path)
    return tmp_path


def _client(responses=(), refresh_weeks=None):
    client = EspnClient(season=2026, league_id=1, refresh_weeks=refresh_weeks)
    client.session = _FakeSession(list(responses))
    return client


def _seed_platform_settings(client, payload, fetched_at):
    path = client._platform_settings_cache_path()
    real_time = time.time
    time.time = lambda: fetched_at
    try:
        cache.write(path, payload, url="https://example.test")
    finally:
        time.time = real_time
    return path


WEEK1_START_S = WEEK1_START / 1000  # the same calendar, expressed as wall-clock seconds


def test_calendar_covers_now_same_period_as_fetch_zero_network_calls(raw_dir, monkeypatch):
    periods = _periods()
    client = _client(responses=[])
    fetched_at = WEEK1_START_S + 10
    _seed_platform_settings(
        client, {"scoringPeriods": periods, "currentScoringPeriod": {"id": 1}}, fetched_at=fetched_at
    )
    monkeypatch.setattr(time, "time", lambda: fetched_at + 60)

    assert client.current_scoring_period() == 1
    assert client.session.calls == []


def test_boundary_crossed_past_the_floor_triggers_one_refetch(raw_dir, monkeypatch):
    periods = _periods()
    fetched_at = WEEK1_START_S + 10
    now_ms = periods[1]["endDate"] + 1000  # just past week 1's original end -> week 2 locally
    now = now_ms / 1000
    assert now - fetched_at > client_module.CALENDAR_RECHECK_FLOOR

    # ESPN pushed week 1's close back an hour past `now` -- the schedule
    # adjustment this refetch exists to pick up.
    healed_periods = [
        periods[0],
        {"id": 1, "startDate": periods[1]["startDate"], "endDate": now_ms + 3600_000},
        {"id": 2, "startDate": now_ms + 3600_000, "endDate": periods[2]["endDate"] + 3600_000},
    ]
    client = _client(
        responses=[
            _FakeResponse(
                200, {"scoringPeriods": healed_periods, "currentScoringPeriod": {"id": 1}}
            )
        ]
    )
    _seed_platform_settings(
        client, {"scoringPeriods": periods, "currentScoringPeriod": {"id": 1}}, fetched_at=fetched_at
    )
    monkeypatch.setattr(time, "time", lambda: now)

    assert client.session.calls == []
    result = client.current_scoring_period()
    assert len(client.session.calls) == 1
    assert result == 1  # refetched calendar shows week 1 still covers `now`


def test_boundary_crossed_within_the_floor_skips_refetch(raw_dir, monkeypatch):
    week1_start = 1000 * 1000  # so week 1 ends at (1000 + 604800) * 1000 ms
    periods = _periods(week1_start=week1_start)
    client = _client(responses=[])
    fetched_at = 1000 + 604800 - 30  # 30s before week 1 closes
    _seed_platform_settings(
        client, {"scoringPeriods": periods, "currentScoringPeriod": {"id": 1}}, fetched_at=fetched_at
    )
    now = fetched_at + 60  # 30s into week 2, but well within the recheck floor
    assert now - fetched_at < client_module.CALENDAR_RECHECK_FLOOR
    monkeypatch.setattr(time, "time", lambda: now)

    assert client.current_scoring_period() == 2
    assert client.session.calls == []


def test_missing_scoring_periods_falls_back_to_a_bounded_ttl(raw_dir, monkeypatch):
    client = _client(
        responses=[_FakeResponse(200, {"scoringPeriods": [], "currentScoringPeriod": {"id": 6}})]
    )
    fetched_at = WEEK1_START_S
    _seed_platform_settings(client, {"currentScoringPeriod": {"id": 5}}, fetched_at=fetched_at)
    now = fetched_at + client_module.CALENDAR_FALLBACK_TTL + 1
    monkeypatch.setattr(time, "time", lambda: now)

    assert client.current_scoring_period() == 6
    assert len(client.session.calls) == 1


def test_current_scoring_period_is_memoised_per_client(raw_dir, monkeypatch):
    periods = _periods()
    client = _client(
        responses=[_FakeResponse(200, {"scoringPeriods": periods, "currentScoringPeriod": {"id": 1}})]
    )
    monkeypatch.setattr(time, "time", lambda: WEEK1_START_S + 60)

    first = client.current_scoring_period()
    second = client.current_scoring_period()
    assert first == second == 1
    assert len(client.session.calls) == 1


def test_failed_refetch_returns_the_stale_week_and_warns_not_raises(raw_dir, monkeypatch, capsys):
    periods = _periods()
    client = _client(
        responses=[_FakeResponse(500, url="https://example.test")] * client_module.MAX_ATTEMPTS
    )
    monkeypatch.setattr(client_module.time, "sleep", lambda s: None)
    fetched_at = WEEK1_START_S + 10
    _seed_platform_settings(
        client, {"scoringPeriods": periods, "currentScoringPeriod": {"id": 1}}, fetched_at=fetched_at
    )
    now_ms = periods[1]["endDate"] + 1000
    now = now_ms / 1000
    assert now - fetched_at > client_module.CALENDAR_RECHECK_FLOOR
    monkeypatch.setattr(time, "time", lambda: now)

    result = client.current_scoring_period()

    assert result == 2  # the locally-resolved week from the stale calendar
    assert len(client.session.calls) == client_module.MAX_ATTEMPTS
    assert "warning" in capsys.readouterr().out


# ---- refresh_weeks: targeted re-pull --------------------------------------


def _seed_roster(client, week, payload):
    key = cache.cache_key(client.season, client.league_id, ["mRoster"], {"scoringPeriodId": week})
    path = cache.cache_path(client.season, ["mRoster"], key)
    cache.write(path, payload, url="https://example.test")
    return path


def test_refresh_weeks_bypasses_the_cache_only_for_the_named_week(raw_dir):
    client = EspnClient(season=2026, league_id=1, refresh_weeks={2})
    client.session = _FakeSession(
        responses=[_FakeResponse(200, {"teams": [], "week": 2})]
    )
    _seed_roster(client, 1, {"teams": [], "week": 1, "cached": True})
    _seed_roster(client, 2, {"teams": [], "week": 2, "cached": True})

    week1 = client.get_league(["mRoster"], scoring_period=1)
    assert week1["cached"] is True
    assert client.session.calls == []

    week2 = client.get_league(["mRoster"], scoring_period=2)
    assert "cached" not in week2
    assert len(client.session.calls) == 1


def test_refresh_weeks_does_not_affect_fetches_without_a_scoring_period(raw_dir):
    client = EspnClient(season=2026, league_id=1, refresh_weeks={1})
    client.session = _FakeSession(responses=[])
    key = cache.cache_key(2026, 1, ["mSettings"], {})
    path = cache.cache_path(2026, ["mSettings"], key)
    cache.write(path, {"cached": True}, url="https://example.test")

    result = client.get_league(["mSettings"])
    assert result == {"cached": True}
    assert client.session.calls == []
