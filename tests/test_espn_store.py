"""The cumulative ESPN transaction store -- the fix for the 2026-09-15
scoring-period truncation, where mTransactions2 returned 214 period-1 rows
at 04:42 ET and 2 period-2 rows at 14:10 ET the same day, and a
straight-through export destroyed the difference everywhere it existed.
Fixtures only, no network, no disk beyond tmp_path."""

import pandas as pd
import pytest

from espn_ff import espn_store


def _row(transaction_id, item_type, player_id, season=2026, scoring_period=1,
         is_pending=False, status="EXECUTED", proposed_date_ms=1_789_000_000_000):
    return {
        "season": season, "transaction_id": transaction_id, "item_type": item_type,
        "player_id": player_id, "scoring_period": scoring_period, "is_pending": is_pending,
        "status": status, "proposed_date_ms": proposed_date_ms,
    }


def test_a_truncated_pull_never_shrinks_the_store():
    """The incident, in miniature: a later pull that returns only the new
    scoring period must not drop the prior period's rows."""
    prior = pd.DataFrame([_row(f"t{i}", "ADD", i, scoring_period=1) for i in range(214)])
    new = pd.DataFrame([_row("new1", "LINEUP", 900, scoring_period=2)])

    merged = espn_store.merge_transactions(prior, new)

    assert len(merged) == 215
    assert set(merged["scoring_period"]) == {1, 2}


def test_paired_add_and_drop_sharing_one_transaction_id_both_survive():
    """A waiver claim is two item rows under one transaction_id. Deduping on
    transaction_id alone would collapse them and lose the drop."""
    rows = pd.DataFrame([
        _row("6fd144f5", "ADD", 4569559),
        _row("6fd144f5", "DROP", 4710714),
    ])
    merged = espn_store.merge_transactions(pd.DataFrame(), rows)
    assert len(merged) == 2
    assert set(merged["item_type"]) == {"ADD", "DROP"}


def test_a_settling_claim_updates_in_place_rather_than_duplicating():
    """is_pending True -> False and status PENDING -> EXECUTED must update
    the existing row, not append a second copy -- a never-overwrite append
    would freeze every claim as pending forever."""
    prior = pd.DataFrame([_row("t1", "ADD", 1, is_pending=True, status="PENDING")])
    new = pd.DataFrame([_row("t1", "ADD", 1, is_pending=False, status="EXECUTED")])

    merged = espn_store.merge_transactions(prior, new)

    assert len(merged) == 1
    assert merged.iloc[0]["is_pending"] is False or merged.iloc[0]["is_pending"] == False  # noqa: E712
    assert merged.iloc[0]["status"] == "EXECUTED"


def test_first_seen_is_preserved_while_last_seen_advances():
    prior = espn_store.merge_transactions(pd.DataFrame(), pd.DataFrame([_row("t1", "ADD", 1)]), seen_at=100.0)
    merged = espn_store.merge_transactions(prior, pd.DataFrame([_row("t1", "ADD", 1)]), seen_at=200.0)

    assert merged.iloc[0]["first_seen_at"] == 100.0
    assert merged.iloc[0]["last_seen_at"] == 200.0


def test_a_row_absent_from_the_new_pull_keeps_its_older_last_seen():
    """This is what lets a reader tell 'ESPN still returns this' from 'last
    seen three pulls ago' -- the store is an observation log, not a mirror."""
    prior = espn_store.merge_transactions(pd.DataFrame(), pd.DataFrame([_row("old", "ADD", 1)]), seen_at=100.0)
    merged = espn_store.merge_transactions(prior, pd.DataFrame([_row("new", "ADD", 2)]), seen_at=200.0)

    by_id = merged.set_index("transaction_id")
    assert by_id.loc["old", "last_seen_at"] == 100.0
    assert by_id.loc["new", "last_seen_at"] == 200.0


def test_an_empty_pull_leaves_the_store_untouched():
    prior = pd.DataFrame([_row("t1", "ADD", 1)])
    assert len(espn_store.merge_transactions(prior, pd.DataFrame())) == 1
    assert len(espn_store.merge_transactions(prior, None)) == 1


def test_one_transaction_touching_a_player_twice_keeps_both_item_rows():
    """A LINEUP move in and out of a slot is two distinct facts under one
    transaction id and one player id -- item_type separates them."""
    rows = pd.DataFrame([
        _row("t1", "LINEUP", 5), _row("t1", "LINEUP", 6),
    ])
    assert len(espn_store.merge_transactions(pd.DataFrame(), rows)) == 2


def test_append_round_trips_through_disk_and_accumulates(tmp_path):
    path = tmp_path / "transactions.parquet"

    espn_store.append_transactions(pd.DataFrame([_row("t1", "ADD", 1, scoring_period=1)]), path=path)
    merged = espn_store.append_transactions(
        pd.DataFrame([_row("t2", "ADD", 2, scoring_period=2)]), path=path
    )

    assert len(merged) == 2
    assert len(espn_store.read_transactions(path)) == 2


def test_read_transactions_on_a_missing_store_is_empty_not_an_error(tmp_path):
    assert espn_store.read_transactions(tmp_path / "nope.parquet").empty


def test_merged_frame_is_sorted_by_period_then_proposed_date(tmp_path):
    rows = pd.DataFrame([
        _row("b", "ADD", 2, scoring_period=2, proposed_date_ms=200),
        _row("a", "ADD", 1, scoring_period=1, proposed_date_ms=300),
        _row("c", "ADD", 3, scoring_period=2, proposed_date_ms=100),
    ])
    merged = espn_store.merge_transactions(pd.DataFrame(), rows)
    merged = espn_store.merge_transactions(merged, rows)
    assert list(merged["transaction_id"]) == ["a", "c", "b"]
