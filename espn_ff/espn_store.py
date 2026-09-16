"""Cumulative on-disk store for ESPN's transaction log.

Every other ESPN export in `data/out/` is a snapshot: drop it and the next
`export` rebuilds it identically, because ESPN re-serves the same state. The
transaction log is the one exception, and this module exists for that single
reason.

`mTransactions2`, called with no `scoringPeriodId` (see `cli.py`'s
`cmd_export`), does not return the season to date. On 2026-09-15 it returned
**214 rows, all `scoring_period == 1`** at a 04:42 ET pull, and **2 rows,
both `scoring_period == 2`** at a 14:10 ET pull the same day (Observed). The
exact scoping rule -- current-period-only, a rolling window, something else
-- is **Inferred**; what is Observed is that the response shrank by 212 rows
across one period rollover.

Because `cli._write` overwrites `data/out/<date>-transactions.csv` wholesale,
that rollover destroyed the week-1 history everywhere it existed: the S3
`latest/out/` copy, and both `archive/out/transactions/` dailies (the archive
held the truncated file twice over, for reasons `scripts/s3_sync.sh`'s
restore-out/archive pairing explains). The only surviving copy was an
untracked file on one laptop.

So: this store is an **observation log, not a mirror**. A row that ESPN has
stopped returning is retained, deliberately. The accepted trade-off is that a
transaction ESPN legitimately voids stays here after it stops being real
upstream -- `last_seen_at` is what lets a reader tell the two apart.
"""

import os
import time

import pandas as pd

from . import config

# NOT `transaction_id` alone. The export is item-grained: one transaction can
# move several players, so a waiver claim is a paired ADD + DROP *sharing* a
# transaction_id (Observed: 6fd144f5-... carries Vele's ADD and Stribling's
# DROP). Deduping on transaction_id alone collapses every such pair to one
# row and silently loses the drop half.
#
# `item_type` is in the key alongside `player_id` because a single
# transaction can legitimately touch one player twice -- a LINEUP move in and
# out of a slot -- and those are distinct facts.
TRANSACTION_KEYS = ["season", "transaction_id", "item_type", "player_id"]

_FIRST_SEEN = "first_seen_at"
_LAST_SEEN = "last_seen_at"


def _atomic_write_parquet(df, path):
    """Same write-partial-then-os.replace shape as odds/store.py. A crash
    mid-write leaves the previous store intact rather than a truncated one --
    which matters more here than there, since this file is unrecoverable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    df.to_parquet(partial, index=False)
    os.replace(partial, path)


def read_transactions(path=None):
    """The cumulative log, or an empty frame when the store does not exist
    yet. Never raises on a missing store -- a first run is not an error."""
    path = path or config.ESPN_TRANSACTIONS
    if not path.exists():
        return pd.DataFrame()
    return pd.read_parquet(path)


def merge_transactions(prior_df, new_df, seen_at=None):
    """Union `prior_df` and `new_df`, keyed on `TRANSACTION_KEYS`.

    `keep="last"` is required, not incidental: a row must be allowed to
    *update*. `is_pending` flips True -> False and `status` moves
    PENDING -> EXECUTED/FAILED_* when a claim settles, so a never-overwrite
    append would freeze every claim in its pending state forever -- the trap
    this module's sibling `report/waivers.py` warns about one layer up.

    It is safe against a truncated pull precisely because this is a union,
    not a replace: rows absent from `new_df` survive. That is the whole fix.

    `first_seen_at` is preserved from the prior row; `last_seen_at` advances
    only for rows present in `new_df`, so a reader can distinguish "ESPN
    still returns this" from "last seen three pulls ago".
    """
    seen_at = time.time() if seen_at is None else seen_at

    if new_df is None or new_df.empty:
        return prior_df if prior_df is not None else pd.DataFrame()

    new_df = new_df.copy()
    new_df[_LAST_SEEN] = seen_at
    if _FIRST_SEEN not in new_df.columns:
        new_df[_FIRST_SEEN] = seen_at

    if prior_df is None or prior_df.empty:
        return new_df.reset_index(drop=True)

    # Carry each already-known row's original first_seen_at onto its
    # incoming counterpart before the dedupe drops the prior copy.
    keyed_prior = prior_df.set_index(TRANSACTION_KEYS)
    if _FIRST_SEEN in keyed_prior.columns and not keyed_prior.index.has_duplicates:
        idx = pd.MultiIndex.from_frame(new_df[TRANSACTION_KEYS])
        carried = keyed_prior[_FIRST_SEEN].reindex(idx).to_numpy()
        new_df[_FIRST_SEEN] = pd.Series(carried, index=new_df.index).fillna(new_df[_FIRST_SEEN])

    combined = pd.concat([prior_df, new_df], ignore_index=True)
    combined = combined.drop_duplicates(subset=TRANSACTION_KEYS, keep="last")
    sort_cols = [c for c in ("scoring_period", "proposed_date_ms") if c in combined.columns]
    if sort_cols:
        combined = combined.sort_values(sort_cols)
    return combined.reset_index(drop=True)


def append_transactions(new_df, path=None, seen_at=None):
    """Merge `new_df` into the store on disk and rewrite it atomically.
    Returns the merged frame -- which is what callers should export, not
    `new_df`, or the truncation this module exists to prevent survives into
    `data/out/`."""
    path = path or config.ESPN_TRANSACTIONS
    merged = merge_transactions(read_transactions(path), new_df, seen_at=seen_at)
    if merged is not None and not merged.empty:
        _atomic_write_parquet(merged, path)
    return merged
