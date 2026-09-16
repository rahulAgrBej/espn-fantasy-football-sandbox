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
# "raw" covers ESPN's cache only (see sync_excludes below), so each vendor's
# raw mirror is listed separately -- otherwise this default, which is what a
# manual seed or a full disaster-recovery restore uses, would silently skip
# the nflverse parquet assets and the Sleeper gz snapshots. Workflows pass
# an explicit subset and never rely on this list.
STATE_SUBTREES=(
    "odds"
    "sleeper"
    "nflverse"
    # data/espn holds the cumulative transaction store, which is the one
    # ESPN artifact that cannot be rebuilt by re-pulling: mTransactions2
    # stops returning a scoring period's rows once the period rolls (see
    # espn_ff/espn_store.py). Omitting it here would mean a
    # disaster-recovery restore silently rebuilds without any transaction
    # history -- the same class of loss this store exists to prevent.
    "espn"
    "raw"
    "raw/nflverse"
    "raw/sleeper"
    "raw/odds"
)

# data/raw is shared: config.py puts ESPN's per-season cache at
# data/raw/<season> but gives each vendor layer its own subdirectory
# (data/raw/{sleeper,nflverse,odds}). Syncing "raw" therefore means *ESPN's
# cache only* -- the vendor subdirectories are addressed explicitly as
# raw/nflverse and friends, and belong to whichever workflow owns that feed.
# Without this, espn.yml's --delete push would delete the nflverse parquet
# mirror whenever it happened to run with a cold copy of it.
VENDOR_RAW_SUBDIRS=(sleeper nflverse odds)

# Never mirror macOS cruft into the bucket. .DS_Store is gitignored, but the
# sync reads the filesystem, not git.
COMMON_EXCLUDES=(--exclude '*.DS_Store' --exclude '.DS_Store')

# Echo the --exclude flags for a subtree: the common ones, plus -- for the
# shared "raw" tree -- the vendor subdirectories that belong to other feeds.
sync_excludes() {
    local sub="$1" d
    printf -- '%s ' "${COMMON_EXCLUDES[@]}"
    [ "$sub" = "raw" ] || return 0
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
        s3 s3 sync "s3://$BUCKET/state/$sub" "$DATA/$sub" $(sync_excludes "$sub") --only-show-errors
    done
}

# --- restore-out: S3 latest/out/ -> local data/out/ -------------------------
# For a read-only caller (e.g. report.yml) that needs a data/out/ export it
# does not itself produce. data/out/ is never part of `restore` -- it is
# purely an archive destination -- so a report reading `latest_export()`
# would otherwise find it empty on a fresh runner even though the morning's
# collection workflow already produced and archived exactly what it needs to
# `latest/out/<dataset>.csv` (cmd_archive's "drop a copy in latest/ for
# convenience" step).
#
# The local filename is re-stamped with today's date so the repo's
# `dd-mm-yyyy-<name>.csv` convention -- and therefore `latest_export()`'s
# filename-date parsing -- still applies. That date is bookkeeping, not a
# freshness claim: latest_export() only uses it to pick the newest among
# multiple local candidates, and a fresh runner will only ever have one.
# Real staleness is judged elsewhere, by each feed's own last_run.json /
# manifest.json / .meta.json, which restore correctly via `restore` already.
#
# A dataset with nothing yet at latest/out/ (e.g. before any export has ever
# run) is skipped rather than aborting the rest -- same "one dead feed must
# not wedge the run" discipline as everywhere else in this pipeline.
cmd_restore_out() {
    local datasets=("$@")
    [ "${#datasets[@]}" -gt 0 ] || return 0

    mkdir -p "$DATA/out"
    local today; today="$(date +%d-%m-%Y)"
    for name in "${datasets[@]}"; do
        say "restore latest/out/$name.csv -> data/out/$today-$name.csv"
        s3 s3 cp "s3://$BUCKET/latest/out/$name.csv" "$DATA/out/$today-$name.csv" \
            --only-show-errors || say "  (no latest/out/$name.csv yet -- skipping)"
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
#
# Unlike cmd_restore, an empty/absent argument here means "own nothing", not
# "own everything" -- restore has no --delete, so defaulting to everything is
# a safe manual-reseed convenience; push-state's --delete makes the same
# default a footgun; a read-only caller (e.g. report.yml) that passes "" must
# not have its (possibly partial) local data/ mirrored over real state.
cmd_push_state() {
    local own=()
    if [ -n "${1:-}" ]; then read -r -a own <<< "$1"; fi

    # "${own[@]}" on a genuinely empty array raises "unbound variable" under
    # set -u on bash < 4.4 (e.g. macOS's default /bin/bash 3.2) -- guard on
    # length rather than relying on a version-dependent expansion.
    [ "${#own[@]}" -gt 0 ] || return 0

    for sub in "${own[@]}"; do
        [ -d "$DATA/$sub" ] || continue
        say "push data/$sub -> state/$sub"
        # shellcheck disable=SC2046  # word splitting is the point here
        s3 s3 sync "$DATA/$sub" "s3://$BUCKET/state/$sub" $(sync_excludes "$sub") --delete --only-show-errors
    done
}

# --- sync-reports: local reports/ <-> S3 reports/ (exact mirror) -----------
# reports/ lives outside data/ entirely (espn_ff/cli.py's cmd_report writes
# to <repo root>/reports/<season>/week-NN/), so neither cmd_archive nor
# cmd_push_state ever sees it. It is git-tracked rather than data/-state, and
# that is what makes a --delete sync safe here despite the general rule
# against --delete over anything but a subtree the caller fully owns:
# actions/checkout restores the complete historical tree from git before the
# workflow runs, and espn_ff report only ever adds to it, so the local copy
# is always the complete, authoritative set at sync time -- a true mirror,
# not a partial one. Deliberately a separate command from cmd_archive, whose
# whole documented contract is "synced WITHOUT --delete"; mixing the two
# semantics into one function would quietly break that invariant for every
# other caller of cmd_archive.
cmd_sync_reports() {
    if [ ! -d "$ROOT/reports" ]; then
        say "no reports/ -- nothing to sync"
        return 0
    fi
    say "sync reports/ -> s3://$BUCKET/reports (exact mirror)"
    s3 s3 sync "$ROOT/reports" "s3://$BUCKET/reports" --delete --only-show-errors
}

# --- pull-reports / pull-summaries: S3 -> a local read cache --------------
# For summary.yml, which has to know which reports already have a summary
# before it can decide what to generate. Both land under .cache/ (gitignored)
# rather than in the working tree, and neither uses --delete: they are read
# caches, not mirrors. That matters -- cmd_sync_reports stays the ONLY
# --delete path over the reports/ prefix, so its header comment above
# explaining why --delete is safe there remains the whole story.
#
# reports/ specifically is NOT read from the local git checkout, even though
# actions/checkout has one: S3 is where report.yml's own mirror step leaves
# the authoritative copy, and reading the same prefix that summaries are
# checked against keeps the two sides of the has-a-summary test consistent.
cmd_pull_reports() {
    local dest="${1:-$ROOT/.cache/s3-reports}"
    mkdir -p "$dest"
    say "pull s3://$BUCKET/reports -> $dest (read cache, no --delete)"
    s3 s3 sync "s3://$BUCKET/reports" "$dest" "${COMMON_EXCLUDES[@]}" --only-show-errors
}

cmd_pull_summaries() {
    local dest="${1:-$ROOT/.cache/s3-summaries}"
    mkdir -p "$dest"
    say "pull s3://$BUCKET/summaries -> $dest (read cache, no --delete)"
    s3 s3 sync "s3://$BUCKET/summaries" "$dest" "${COMMON_EXCLUDES[@]}" --only-show-errors
}

# --- sync-summaries: local summaries/ -> S3 summaries/ (append-only) -------
# **No --delete, and that is load-bearing.** Unlike reports/, summaries/ is
# gitignored, so actions/checkout restores none of its history and the local
# tree holds only what this run produced. A --delete mirror from a tree that
# partial would wipe every prior summary in the bucket on every run -- the
# exact state/ vs archive/ distinction this script's header spells out, with
# summaries/ firmly on the archive/ side.
#
# Note the asymmetry with cmd_pull_summaries: that one fills .cache/, this
# one pushes from summaries/. The two directories are deliberately different
# (see espn_ff/ai/summarize.py's module docstring) -- the read cache holds
# the full history, the output tree holds one run.
cmd_sync_summaries() {
    if [ ! -d "$ROOT/summaries" ]; then
        say "no summaries/ -- nothing to sync"
        return 0
    fi
    say "sync summaries/ -> s3://$BUCKET/summaries (append-only)"
    s3 s3 sync "$ROOT/summaries" "s3://$BUCKET/summaries" "${COMMON_EXCLUDES[@]}" --only-show-errors
}

# --- receipt: record what this run actually did ---------------------------
# Written even when the command failed, so a budget-aborted odds job or an
# expired-cookie ESPN job leaves a trail. stale flags come from the feeds'
# own last_run.json files rather than from the exit code.
#
# Keyed by command, not just by run id: one workflow run can invoke ff-run
# several times (odds.yml does a --dry-run preflight, then the metered job,
# then credits), and a single per-run key meant each receipt overwrote the
# last -- so the metered job's own trail was lost to the trailing credits
# call, which is exactly the record worth keeping.
cmd_receipt() {
    local workflow="${1:?workflow}" run_id="${2:?run_id}" cmd="${3:-}" code="${4:-0}"
    local tmp; tmp="$(mktemp)"

    # Slug the command for the object name: "odds slate --dry-run" ->
    # "odds-slate-dry-run"; a multi-line command -> "sleeper-status".
    local slug
    slug="$(printf '%s' "$cmd" | tr '\n' ' ' \
            | sed -e 's/--//g' -e 's/[^A-Za-z0-9]\{1,\}/-/g' \
                  -e 's/^-//' -e 's/-$//' | cut -c1-60)"
    [ -n "$slug" ] || slug="run"
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
    s3 s3 cp "$tmp" "s3://$BUCKET/logs/runs/$workflow/$run_id/$slug.json" --only-show-errors
    rm -f "$tmp"
    say "receipt -> logs/runs/$workflow/$run_id/$slug.json"
}

case "${1:-}" in
    restore)        shift; cmd_restore "${1:-}" ;;
    restore-out)    shift; cmd_restore_out "$@" ;;
    archive)        shift; cmd_archive ;;
    push-state)     shift; cmd_push_state "${1:-}" ;;
    sync-reports)   shift; cmd_sync_reports ;;
    pull-reports)   shift; cmd_pull_reports "${1:-}" ;;
    pull-summaries) shift; cmd_pull_summaries "${1:-}" ;;
    sync-summaries) shift; cmd_sync_summaries ;;
    receipt)        shift; cmd_receipt "$@" ;;
    *) echo "usage: $0 {restore [subtrees]|restore-out <dataset>...|archive|push-state [owned-subtrees]|sync-reports|pull-reports [dest]|pull-summaries [dest]|sync-summaries|receipt <workflow> <run_id> <cmd> <code>}" >&2; exit 64 ;;
esac
