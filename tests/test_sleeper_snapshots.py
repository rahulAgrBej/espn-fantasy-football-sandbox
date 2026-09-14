"""Sleeper snapshot store: slimming, the fetch guard, retention -- fixtures
only, no network."""

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from espn_ff.sleeper import snapshots

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def raw_players():
    return json.loads((FIXTURES / "sleeper_players_small.json").read_text())


class _FakeClient:
    def __init__(self, raw=None, error=None):
        self._raw = raw
        self._error = error

    def get_players(self):
        if self._error is not None:
            raise self._error
        return self._raw


@pytest.fixture
def sleeper_store(tmp_path, monkeypatch):
    """Point every path helper at a scratch dir for this test, and drop the
    2,000-player guard threshold to fit the small fixture (5 active
    players) -- the guard's behaviour is exercised on its own terms below."""
    monkeypatch.setattr(snapshots.config, "SLEEPER_DIR", tmp_path)
    monkeypatch.setattr(snapshots.config, "SLEEPER_RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(snapshots.config, "SLEEPER_SLIM_DIR", tmp_path / "slim")
    monkeypatch.setattr(snapshots, "MIN_ACTIVE_PLAYERS", 3)
    return tmp_path


def test_slim_players_drops_inactive_teamless_rows(raw_players):
    df = snapshots.slim_players(raw_players, fetched_at=1234.0)
    assert set(df["sleeper_id"]) == {"1001", "1002", "1003", "1004", "1006"}
    assert list(df.columns) == snapshots.SLIM_COLUMNS


def test_fetch_players_short_circuits_when_todays_snapshot_exists(sleeper_store, raw_players):
    today = date(2026, 9, 14)
    (sleeper_store / "slim").mkdir(parents=True)
    existing = snapshots.slim_players(raw_players, fetched_at=1.0)
    existing.to_csv(snapshots.slim_path(today), index=False)

    client = _FakeClient(raw=raw_players)
    df, from_cache = snapshots.fetch_players(client=client, today=today)
    assert from_cache is True
    assert len(df) == len(existing)


def test_fetch_players_writes_slim_raw_and_last_run_on_success(sleeper_store, raw_players):
    today = date(2026, 9, 14)
    client = _FakeClient(raw=raw_players)
    df, from_cache = snapshots.fetch_players(client=client, today=today)

    assert from_cache is False
    assert snapshots.slim_path(today).exists()
    assert snapshots.raw_path(today).exists()

    last_run = json.loads(snapshots.last_run_path().read_text())
    assert last_run["stale"] is False
    assert last_run["player_count"] == len(df)


def test_fetch_players_guard_preserves_prior_snapshot_on_request_failure(sleeper_store, raw_players):
    today = date(2026, 9, 14)
    yesterday = today - timedelta(days=1)
    (sleeper_store / "slim").mkdir(parents=True)
    prior = snapshots.slim_players(raw_players, fetched_at=1.0)
    prior.to_csv(snapshots.slim_path(yesterday), index=False)

    client = _FakeClient(error=RuntimeError("boom"))
    df, from_cache = snapshots.fetch_players(client=client, refresh=True, today=today)

    assert not snapshots.slim_path(today).exists()
    assert snapshots.slim_path(yesterday).exists()  # untouched
    assert len(df) == len(prior)  # falls back to the last good snapshot

    last_run = json.loads(snapshots.last_run_path().read_text())
    assert last_run["stale"] is True
    assert "boom" in last_run["reason"]


def test_fetch_players_guard_on_thin_active_count(sleeper_store):
    today = date(2026, 9, 14)
    thin_raw = {
        "1": {"player_id": "1", "full_name": "Solo Active Guy", "team": "GB", "position": "WR", "active": True}
    }
    client = _FakeClient(raw=thin_raw)
    df, from_cache = snapshots.fetch_players(client=client, refresh=True, today=today)

    assert not snapshots.slim_path(today).exists()
    last_run = json.loads(snapshots.last_run_path().read_text())
    assert last_run["stale"] is True
    assert "active players" in last_run["reason"]


def test_fetch_players_cold_start_with_no_prior_snapshot(sleeper_store):
    today = date(2026, 9, 14)
    client = _FakeClient(error=RuntimeError("network down"))
    df, from_cache = snapshots.fetch_players(client=client, today=today)
    assert df.empty
    assert list(df.columns) == snapshots.SLIM_COLUMNS


def test_prune_keeps_only_the_most_recent_n(tmp_path):
    for i in range(1, 16):
        (tmp_path / f"2026-01-{i:02d}-players.csv").write_text("sleeper_id\n")
    snapshots._prune_dir(tmp_path, "*-players.csv", snapshots.KEEP_SLIM)
    remaining = sorted(p.name for p in tmp_path.glob("*-players.csv"))
    assert len(remaining) == snapshots.KEEP_SLIM
    assert remaining[-1] == "2026-01-15-players.csv"


def test_list_slim_dates_and_read_slim_roundtrip(sleeper_store, raw_players):
    today = date(2026, 9, 14)
    client = _FakeClient(raw=raw_players)
    snapshots.fetch_players(client=client, today=today)

    dates = snapshots.list_slim_dates()
    assert dates == [today]
    assert len(snapshots.read_slim(today)) == 5
    assert snapshots.read_slim(today - timedelta(days=1)) is None
