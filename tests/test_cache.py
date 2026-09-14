"""Cache keying and the completed-week-vs-live-week freshness rule."""

import time

import pytest

from espn_ff import cache


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
