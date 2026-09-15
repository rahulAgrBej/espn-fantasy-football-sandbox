"""espn_ff.odds.store -- disk and freshness, fixtures only, no network."""

import pandas as pd
import pytest

from espn_ff.odds import store


@pytest.fixture
def odds_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(store.config, "ODDS_DIR", tmp_path)
    monkeypatch.setattr(store.config, "ODDS_RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(store.config, "ODDS_SCORING", tmp_path / "league_scoring.json")
    return tmp_path


def test_archive_raw_writes_text_verbatim_and_leaves_no_partial(odds_paths):
    dest = store.archive_raw('{"a": 1}', "2026-09", "run1", "featured_odds", None, "20260918T120000Z")
    assert dest.exists()
    assert dest.read_text() == '{"a": 1}'
    assert not dest.with_name(dest.name + ".partial").exists()


def test_archive_raw_partitions_by_event_id(odds_paths):
    dest = store.archive_raw("{}", "2026-09", "run1", "event_odds", "evt123", "ts1")
    assert "evt123" in str(dest)


def test_append_snapshot_dedupes_on_key_keeping_the_later_capture(odds_paths, tmp_path):
    path = tmp_path / "player_props.parquet"
    first = pd.DataFrame([
        {"captured_at": "2026-09-18T10:00:00Z", "event_id": "e1", "player_name": "Jayden Reed", "market": "player_receptions", "book": "draftkings", "outcome_name": "Over", "point": 4.5},
    ])
    store.append_snapshot(first, path, store.PROPS_DEDUPE_KEYS)

    second = pd.DataFrame([
        {"captured_at": "2026-09-18T10:00:00Z", "event_id": "e1", "player_name": "Jayden Reed", "market": "player_receptions", "book": "draftkings", "outcome_name": "Over", "point": 5.0},
        {"captured_at": "2026-09-19T10:00:00Z", "event_id": "e1", "player_name": "Jayden Reed", "market": "player_receptions", "book": "draftkings", "outcome_name": "Over", "point": 5.5},
    ])
    combined = store.append_snapshot(second, path, store.PROPS_DEDUPE_KEYS)

    assert len(combined) == 2
    same_key_row = combined[combined["captured_at"] == "2026-09-18T10:00:00Z"].iloc[0]
    assert same_key_row["point"] == 5.0  # exact-same-key rewrite: the latest write for that key wins


def test_append_snapshot_keeps_both_sides_of_a_two_sided_market(odds_paths, tmp_path):
    """Regression: outcome_name must be part of the dedupe key. Over and
    Under (or Yes and No) share every other column within one capture, so
    omitting outcome_name let one side silently clobber the other --
    verified against a real team_totals.parquet capture where the totals
    market's Over rows were entirely lost this way."""
    path = tmp_path / "player_props.parquet"
    rows = pd.DataFrame([
        {"captured_at": "2026-09-18T10:00:00Z", "event_id": "e1", "player_name": "Jayden Reed", "market": "player_receptions", "book": "draftkings", "outcome_name": "Over", "point": 4.5},
        {"captured_at": "2026-09-18T10:00:00Z", "event_id": "e1", "player_name": "Jayden Reed", "market": "player_receptions", "book": "draftkings", "outcome_name": "Under", "point": 4.5},
    ])
    combined = store.append_snapshot(rows, path, store.PROPS_DEDUPE_KEYS)
    assert set(combined["outcome_name"]) == {"Over", "Under"}


def test_append_snapshot_never_shrinks_the_file_on_disk(odds_paths, tmp_path):
    path = tmp_path / "player_props.parquet"
    store.append_snapshot(
        pd.DataFrame([{"captured_at": "d1", "event_id": "e1", "player_name": "A", "market": "m", "book": "b", "outcome_name": "Over", "point": 1}]),
        path, store.PROPS_DEDUPE_KEYS,
    )
    before = len(pd.read_parquet(path))

    store.append_snapshot(pd.DataFrame(columns=["captured_at", "event_id", "player_name", "market", "book", "outcome_name", "point"]), path, store.PROPS_DEDUPE_KEYS)
    after = len(pd.read_parquet(path))
    assert after >= before


def test_write_and_read_last_run_carries_stale_and_reason(odds_paths):
    store.write_last_run("props_primary", credits_spent=0, stale=True, reason="no props returned; markets likely not open yet")
    record = store.read_last_run("props_primary")
    assert record["stale"] is True
    assert "not open yet" in record["reason"]


def test_last_run_is_keyed_per_job_so_one_jobs_staleness_never_masks_another(odds_paths):
    store.write_last_run("slate_context", credits_spent=2, stale=False)
    store.write_last_run("props_primary", credits_spent=0, stale=True, reason="markets not open")

    assert store.read_last_run("slate_context")["stale"] is False
    assert store.read_last_run("props_primary")["stale"] is True


def test_write_and_read_league_scoring_round_trips(odds_paths):
    store.write_league_scoring({"stat_id": 42, "points": 0.1})
    assert store.read_league_scoring() == {"stat_id": 42, "points": 0.1}


def test_read_league_scoring_with_nothing_on_disk_returns_none(odds_paths):
    assert store.read_league_scoring() is None
