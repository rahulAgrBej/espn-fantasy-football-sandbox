#!/usr/bin/env bash
# Sync this repo's data/ tree to and from S3.
#
# GitHub Actions runners start empty, but four things under data/ are
# load-bearing across runs -- the Odds credit ledger (the only record of
# month-to-date spend), the Odds snapshot parquets (the API has no historical
# endpoint, so a lost snapshot can never be re-fetched), the daily Sleeper
# snapshots that practice_trajectory is reconstructed from, and nflverse's
# ETag manifest. S3 is therefore the state store, not just an output sink.
#
# Two prefixes with deliberately different semantics:
#
#   state/    exact mirror of the stateful subtrees, synced WITH --delete
#   archive/  append-only, synced WITHOUT --delete
#
# The split matters because sleeper/snapshots.py:_prune keeps only the last
# KEEP_SLIM=10 snapshots locally. A single --delete sync over one prefix
# would replicate that pruning into S3 and destroy the long-run history.
#
# Never run this under `set -x`: ODDS_API_KEY may be present in the
# environment, and the odds client's own redaction does not cover a shell
# trace.

set -euo pipefail

BUCKET="${S3_BUCKET:?S3_BUCKET not set}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="$ROOT/data"

# Subtrees that make up "state". Anything not listed here is either derived
# (data/out) or reproducible, and is archived rather than mirrored.
STATE_SUBTREES=(
    "odds"
    "sleeper"
    "nflverse"
    "raw"
)

# data/raw is shared: config.py puts ESPN's per-season cache at
# data/raw/<season> but gives each vendor layer its own subdirectory
# (data/raw/{sleeper,nflverse,odds}). Syncing "raw" therefore means *ESPN's
# cache only* -- the vendor subdirectories are addressed explicitly as
# raw/nflverse and friends, and belong to whichever workflow owns that feed.
# Without this, espn.yml's --delete push would delete the nflverse parquet
# mirror whenever it happened to run with a cold copy of it.
VENDOR_RAW_SUBDIRS=(sleeper nflverse odds)

# Echo the --exclude flags that scope a "raw" sync to the ESPN cache.
raw_excludes() {
    local sub="$1"
    [ "$sub" = "raw" ] || return 0
    local d
    for d in "${VENDOR_RAW_SUBDIRS[@]}"; do
        printf -- '--exclude %s/* ' "$d"
    done
}

say() { printf '  [s3] %s\n' "$*"; }

# aws s3 sync, with the profile flag only when AWS_PROFILE is set. CI uses
# OIDC and has no profile; the laptop uses AWS_PROFILE=default.
s3() {
    if [ -n "${AWS_PROFILE:-}" ]; then
        aws --profile "$AWS_PROFILE" "$@"
    else
        aws "$@"
    fi
}

# --- restore: S3 state/ -> local data/ ------------------------------------
# Takes an optional space-separated list of subtrees to limit the restore to
# (e.g. "raw" for the Sunday live-scoring job, which needs only the ESPN
# cache). Empty means all of STATE_SUBTREES.
cmd_restore() {
    local want=("${STATE_SUBTREES[@]}")
    if [ -n "${1:-}" ]; then read -r -a want <<< "$1"; fi

    mkdir -p "$DATA"
    for sub in "${want[@]}"; do
        say "restore state/$sub -> data/$sub"
        # shellcheck disable=SC2046  # word splitting is the point here
        s3 s3 sync "s3://$BUCKET/state/$sub" "$DATA/$sub" $(raw_excludes "$sub") --only-show-errors
    done
}

# --- archive: local -> S3 archive/ (append-only, never deletes) ------------
cmd_archive() {
    # Output CSVs land as data/out/dd-mm-yyyy-<name>.csv. Re-key them to
    # archive/out/<dataset>/YYYY-MM-DD.csv so a dataset's history sorts
    # lexically, and drop a copy in latest/ for convenience.
    if [ -d "$DATA/out" ]; then
        for f in "$DATA"/out/*.csv; do
            [ -e "$f" ] || continue
            local base dd mm yyyy dataset
            base="$(basename "$f" .csv)"
            dd="${base:0:2}"; mm="${base:3:2}"; yyyy="${base:6:4}"
            dataset="${base:11}"
            say "archive $dataset ($yyyy-$mm-$dd)"
            s3 s3 cp "$f" "s3://$BUCKET/archive/out/$dataset/$yyyy-$mm-$dd.csv" --only-show-errors
            s3 s3 cp "$f" "s3://$BUCKET/latest/out/$dataset.csv" --only-show-errors
        done
    fi

    # Sleeper slim snapshots are pruned locally to the last 10; the archive
    # keeps every one of them.
    if [ -d "$DATA/sleeper/slim" ]; then
        say "archive sleeper snapshots"
        s3 s3 sync "$DATA/sleeper/slim" "s3://$BUCKET/archive/sleeper/slim" --only-show-errors
    fi

    # Odds raw payloads -- unrecoverable once gone, so they are archived
    # before the state mirror runs.
    if [ -d "$DATA/raw/odds" ]; then
        say "archive odds raw payloads"
        s3 s3 sync "$DATA/raw/odds" "s3://$BUCKET/archive/odds/raw" --only-show-errors
    fi
}

# --- push-state: local data/ -> S3 state/ (exact mirror) -------------------
# Takes the subtrees this caller OWNS. The --delete is what makes the mirror
# exact -- and what makes it dangerous to run over a subtree someone else is
# writing. Two workflows scheduled in the same minute (e.g. Monday's ESPN
# close-the-book and the odds `results` job, which both read data/raw) would
# otherwise race: the second to finish would push its older copy over the
# first's fresh one. Each workflow therefore pushes only what it owns, and
# restores the rest read-only.
cmd_push_state() {
    local own=("${STATE_SUBTREES[@]}")
    if [ -n "${1:-}" ]; then read -r -a own <<< "$1"; fi

    for sub in "${own[@]}"; do
        [ -d "$DATA/$sub" ] || continue
        say "push data/$sub -> state/$sub"
        # shellcheck disable=SC2046  # word splitting is the point here
        s3 s3 sync "$DATA/$sub" "s3://$BUCKET/state/$sub" $(raw_excludes "$sub") --delete --only-show-errors
    done
}

# --- receipt: record what this run actually did ---------------------------
# Written even when the command failed, so a budget-aborted odds job or an
# expired-cookie ESPN job leaves a trail. stale flags come from the feeds'
# own last_run.json files rather than from the exit code.
cmd_receipt() {
    local workflow="${1:?workflow}" run_id="${2:?run_id}" cmd="${3:-}" code="${4:-0}"
    local tmp; tmp="$(mktemp)"
    ROOT="$ROOT" WORKFLOW="$workflow" RUN_ID="$run_id" CMD="$cmd" CODE="$code" \
    python3 - > "$tmp" <<'PY'
import json, os, pathlib, time

root = pathlib.Path(os.environ["ROOT"])
def read(p):
    f = root / "data" / p
    try:
        return json.loads(f.read_text())
    except Exception:
        return None

print(json.dumps({
    "workflow": os.environ["WORKFLOW"],
    "run_id": os.environ["RUN_ID"],
    "command": os.environ["CMD"],
    "exit_code": int(os.environ["CODE"]),
    "recorded_at": time.time(),
    "odds_last_run": read("odds/last_run.json"),
    "sleeper_last_run": read("sleeper/last_run.json"),
}, indent=2, default=str))
PY
    s3 s3 cp "$tmp" "s3://$BUCKET/logs/runs/$workflow/$run_id.json" --only-show-errors
    rm -f "$tmp"
    say "receipt -> logs/runs/$workflow/$run_id.json"
}

case "${1:-}" in
    restore)    shift; cmd_restore "${1:-}" ;;
    archive)    shift; cmd_archive ;;
    push-state) shift; cmd_push_state "${1:-}" ;;
    receipt)    shift; cmd_receipt "$@" ;;
    *) echo "usage: $0 {restore [subtrees]|archive|push-state [owned-subtrees]|receipt <workflow> <run_id> <cmd> <code>}" >&2; exit 64 ;;
esac
