"""nflverse parquet store: manifest, freshness short-circuit, schema
assertion, guard behaviour -- fixtures only, no network."""

import time

import pandas as pd
import pytest

from espn_ff.nflverse import store
from espn_ff.nflverse.client import NflverseError
from espn_ff.nflverse.datasets import DATASETS, NflverseSchemaError, assert_schema


class _FakeNflverseClient:
    """Records every download() call so a test can assert one was never
    made -- the point of the timestamp short-circuit."""

    def __init__(self, timestamps=None, payloads=None):
        self._timestamps = timestamps or {}
        self._payloads = payloads or {}  # tag -> DataFrame to write on "download"
        self.download_calls = []

    def get_timestamp(self, tag):
        return self._timestamps.get(tag)

    def download(self, tag, filename, dest, etag=None):
        self.download_calls.append((tag, filename, etag))
        if tag not in self._payloads:
            return None, None, "missing"
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._payloads[tag].to_parquet(dest, index=False)
        return dest, f"etag-{tag}", "updated"


def _players_df():
    return pd.DataFrame(
        [{"gsis_id": "00-0001", "espn_id": "111", "pfr_id": "AbcDe00",
          "display_name": "Some Player", "position": "WR", "status": "ACT"}]
    )


@pytest.fixture
def nflverse_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(store.config, "NFLVERSE_DIR", tmp_path)
    monkeypatch.setattr(store.config, "NFLVERSE_RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(store.config, "NFLVERSE_MANIFEST", tmp_path / "manifest.json")
    return tmp_path


def test_refresh_skips_download_when_timestamp_unchanged(nflverse_paths):
    dest = store.local_path("players")
    dest.parent.mkdir(parents=True, exist_ok=True)
    _players_df().to_parquet(dest, index=False)
    store.write_manifest({"players": {"last_updated": "2026-09-14 08:00:00 EDT", "etag": "e1", "rows": 1}})

    client = _FakeNflverseClient(timestamps={"players": "2026-09-14 08:00:00 EDT"})
    results = store.refresh(names=["players"], client=client)

    assert results[0].status == "unchanged"
    assert client.download_calls == []  # the whole point of the short-circuit


def test_refresh_downloads_when_timestamp_changed(nflverse_paths):
    dest = store.local_path("players")
    dest.parent.mkdir(parents=True, exist_ok=True)
    _players_df().to_parquet(dest, index=False)
    store.write_manifest({"players": {"last_updated": "2026-09-13 08:00:00 EDT", "etag": "old"}})

    client = _FakeNflverseClient(
        timestamps={"players": "2026-09-14 08:00:00 EDT"},
        payloads={"players": _players_df()},
    )
    results = store.refresh(names=["players"], client=client)

    assert results[0].status == "updated"
    assert len(client.download_calls) == 1
    manifest = store.read_manifest()
    assert manifest["players"]["last_updated"] == "2026-09-14 08:00:00 EDT"
    assert manifest["players"]["etag"] == "etag-players"


def test_refresh_404_on_optional_dataset_returns_missing_without_raising(nflverse_paths):
    client = _FakeNflverseClient(timestamps={})  # no timestamp.json -> 404-equivalent
    results = store.refresh(names=["injuries"], seasons=[2026], client=client)

    assert results[0].status == "missing"
    manifest = store.read_manifest()
    assert manifest["injuries/2026"]["status"] == "missing"


def test_load_optional_dataset_with_nothing_on_disk_returns_empty_frame(nflverse_paths, capsys):
    df = store.load("injuries", season=2026)
    assert df.empty
    assert "warning" in capsys.readouterr().out


def test_load_required_dataset_with_nothing_on_disk_raises(nflverse_paths):
    with pytest.raises(NflverseError):
        store.load("players")


def test_schema_assertion_names_the_missing_column():
    bad_df = pd.DataFrame([{"gsis_id": "00-0001", "espn_id": "111", "pfr_id": "AbcDe00", "display_name": "X", "position": "WR"}])
    with pytest.raises(NflverseSchemaError) as exc_info:
        assert_schema("players", bad_df.columns)
    assert "status" in str(exc_info.value)


def test_schema_assertion_ignores_unknown_extra_columns():
    df = _players_df()
    df["some_new_upstream_column"] = "whatever"
    assert_schema("players", df.columns)  # must not raise


def test_load_asserts_schema_and_raises_on_a_malformed_file(nflverse_paths):
    dest = store.local_path("players")
    dest.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{"gsis_id": "00-0001"}]).to_parquet(dest, index=False)  # missing required cols
    with pytest.raises(NflverseSchemaError):
        store.load("players")


def test_depth_charts_load_collapses_to_the_latest_dt_per_team(nflverse_paths):
    dest = store.local_path("depth_charts", season=2026)
    dest.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {"dt": "2026-09-10T00:00:00Z", "team": "GB", "gsis_id": "00-0001", "pos_abb": "WR", "pos_rank": 1},
            {"dt": "2026-09-14T00:00:00Z", "team": "GB", "gsis_id": "00-0002", "pos_abb": "WR", "pos_rank": 1},
            {"dt": "2026-09-12T00:00:00Z", "team": "MIN", "gsis_id": "00-0003", "pos_abb": "WR", "pos_rank": 1},
        ]
    ).to_parquet(dest, index=False)

    df = store.load("depth_charts", season=2026)
    assert list(df["dt"]) == ["2026-09-14T00:00:00Z"]


def test_is_stale_boundary():
    fresh = {"fetched_at": time.time() - 3600}  # 1h ago
    stale = {"fetched_at": time.time() - 37 * 3600}  # 37h ago
    assert store.is_stale(fresh, hours=36) is False
    assert store.is_stale(stale, hours=36) is True
    assert store.is_stale(None, hours=36) is True
    assert store.is_stale({}, hours=36) is True


def test_download_partial_swap_leaves_no_truncated_file(tmp_path, monkeypatch):
    """The .partial staging file must never survive a successful download,
    and a killed write must never leave a truncated dest."""
    from espn_ff.nflverse.client import NflverseClient

    class _FakeResponse:
        status_code = 200
        headers = {"ETag": '"abc"'}
        content = b"parquet-bytes"

        def raise_for_status(self):
            pass

    client = NflverseClient()
    monkeypatch.setattr(client.session, "get", lambda url, headers=None, timeout=None: _FakeResponse())

    dest = tmp_path / "players" / "players.parquet"
    path, etag, status = client.download("players", "players.parquet", dest)

    assert status == "updated"
    assert etag == '"abc"'
    assert dest.read_bytes() == b"parquet-bytes"
    assert not dest.with_name(dest.name + ".partial").exists()


def test_all_datasets_are_registered_with_at_least_one_required_column():
    for name, dataset in DATASETS.items():
        assert dataset.required, f"{name} has no required columns declared"
