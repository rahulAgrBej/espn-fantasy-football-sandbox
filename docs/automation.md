# Automation

This is the operational companion to
`docs/data-collection-weekly-schedule.md`, which says *what should be
pulled on each day of the week and why*. This document answers the
follow-on question: **how that schedule actually runs without a human, and
where the data goes.**

Six GitHub Actions workflows run the schedule; an S3 bucket holds both the
outputs and — the part that is easy to get wrong — the *state* those
workflows need in order to produce anything meaningful.

The clock is not GitHub's. AWS EventBridge Scheduler dispatches every slot
through the GitHub REST API, because GitHub's own `on.schedule` proved
undeliverable here — see `docs/aws-scheduling.md`, which owns the trigger
path, the schedule table and the dispatch credential. This document picks up
at the point a run starts.

## How to read this

Cadence and behavioural claims below are tagged the same three ways as
`docs/data-sources.md`:

- **Observed** — measured from an artifact (a workflow run, an object in
  the bucket).
- **Documented** — asserted by a vendor, by GitHub's own docs, or by a
  docstring in this repo.
- **Inferred** — deduced from how the code behaves, not published anywhere.

## Overview

Times are ET and hold year-round: the schedules are pinned to
`America/New_York`, so they no longer shift an hour when DST ends.

| Workflow | Dispatched at (ET) | Runs | Metered |
|---|---|---|---|
| `sleeper.yml` | 08:11 daily | `sleeper`, `status` | no |
| `nflverse.yml` | 09:23 / 13:23 / 18:23 daily | `nflverse`, `features` | no |
| `nflverse.yml` | Thu 09:53 | `nflverse --force`, `features` | no |
| `espn.yml` | Mon 09:08, Tue 09:08 | `pull --refresh`, `export --refresh` | no |
| `espn.yml` | Sun 13:08 – Mon 00:38, every 30 min | same | no |
| `odds.yml` | five slots, see below | `odds <job>`, `projections` | **yes** |
| `health.yml` | 07:04 / 19:04 daily, Sun 12:04 | `probe` | no |
| `report.yml` | Mon 10:30 | `report --day monday` | no |
| `tests.yml` | on push / PR (GitHub's own trigger) | `pytest` | no |

`tests.yml` is the only workflow GitHub still triggers by itself; it runs on
`push` and `pull_request`, which are events rather than a clock. Every other
row is an EventBridge schedule — `docs/aws-scheduling.md` has the cron
expressions, the inputs each one sends, and the two 30-minute ordering gaps
(espn→odds on Mon/Tue, nflverse routine→forced on Thursday) that must survive
any retiming.

The `odds.yml` slots map one-to-one onto the five-job schedule in
`docs/odds-budget.md`: `slate` (Tue 09:38 ET), `props` (Thu 10:08),
`line_movement` (Fri 10:08), `pre_lock` (Sun 10:38), `results` (Mon 09:38).
That document owns the credit invariant, the cost table, and the run
budgets; this one does not restate them.

## Why S3 holds state, not just output

The non-obvious part of running this on ephemeral runners: four things
under `data/` are inputs to the next run, not products of this one. A
runner that starts empty does not merely re-fetch — it produces *different,
worse* answers.

| State | What breaks without it |
|---|---|
| `data/sleeper/slim/*.csv` | `practice_trajectory` reads `— / — / —`. It is reconstructed from our own consecutive daily snapshots; Sleeper publishes no history to backfill from *(Documented — `espn_ff/sleeper/snapshots.py`)*. |
| `data/odds/ledger.db` | The only record of month-to-date credit spend. A reset ledger defeats the 500-credit guard *(Documented — `docs/odds-budget.md`)*. |
| `data/odds/*.parquet` | Append-only snapshot archives. The Odds API has no historical endpoint, so a lost snapshot can never be re-fetched *(Documented)*. |
| `data/nflverse/manifest.json` | ETags and `last_updated`; without them every run re-downloads all six assets instead of taking a 304 *(Inferred — from `nflverse/store.py`'s short-circuit)*. |
| `data/raw/<season>/` | The ESPN cache, including the season calendar `current_scoring_period()` resolves the live week from *(Inferred — from `client.py:163-205`)*. |

So each run is: **restore state → run → archive outputs → push state back.**
`scripts/s3_sync.sh` implements those four verbs and
`.github/actions/ff-run` sequences them.

## Bucket layout

```
s3://espn-ff-data-2026/
  state/      exact mirror of the stateful data/ subtrees   (synced WITH --delete)
  archive/    append-only history, never deleted            (synced WITHOUT --delete)
  latest/     newest copy of each output dataset
  reports/    exact mirror of the repo's reports/ tree       (synced WITH --delete)
  logs/runs/  one receipt per workflow run
```

`reports/` is a different kind of "exact mirror" than `state/`: it is safe
to sync with `--delete` not because one workflow owns it in the
one-writer-per-subtree sense, but because it mirrors a git-tracked
directory — `actions/checkout` restores the complete history before
`report.yml` runs, so the local copy is always the full, authoritative set
by the time it syncs.

**The two prefixes exist because local pruning is not the archive policy.**
`espn_ff/sleeper/snapshots.py:_prune` keeps only the last
`KEEP_SLIM = 10` snapshots on disk. A single `--delete` sync over one
prefix would faithfully replicate that pruning into S3 and destroy the
long-run history — so `state/` mirrors exactly (it must, or a deleted file
would resurrect on the next restore) while `archive/` only ever gains
objects.

### One writer per subtree

`state/` is mirrored with `--delete`, which makes it exact and makes it
dangerous: two workflows pushing the same subtree would race, and the later
push would win with older content. So each workflow **restores** whatever it
needs to read but **pushes back only what it owns**:

| Workflow | Restores | Owns |
|---|---|---|
| `espn.yml` | `raw` | `raw` |
| `sleeper.yml` | `sleeper raw raw/sleeper` | `sleeper`, `raw/sleeper` |
| `nflverse.yml` | `nflverse raw raw/nflverse` | `nflverse`, `raw/nflverse` |
| `odds.yml` | `odds raw raw/odds` | `odds`, `raw/odds` |
| `report.yml` | `raw sleeper nflverse raw/nflverse`, plus `latest/out/{matchups,weekly-rosters,player-pool,roster-slots}` | none of `data/` — see below |

`report.yml` is the odd one out twice over: it is the first workflow that
passes an empty `push-paths` and means it (it restores several subtrees
read-only and owns none of them — `s3_sync.sh`'s `push-state` treats an
empty argument as "push nothing," not its old default of "push everything"),
and it also reads a slice of `data/out/` that `restore` never covers at all.
`data/out/` is purely an archive destination for every other workflow —
`report.yml` seeds it from `s3://$BUCKET/latest/out/<dataset>.csv` instead
(the "newest copy" convenience prefix from the bucket layout below), via a
new `s3_sync.sh restore-out` subcommand.

`report.yml` also owns state genuinely outside `data/`: it writes to
`reports/<season>/week-NN/`, which is git-tracked rather than S3-state, and
mirrors that same tree to a new top-level `s3://$BUCKET/reports/` prefix
(via a new `sync-reports` subcommand) after committing it. This is the only
workflow with `contents: write` on this repo.

`data/raw` is shared, which is the subtlety worth knowing: `config.py` puts
ESPN's per-season cache at `data/raw/<season>` but gives each vendor layer
its own sibling directory (`data/raw/{sleeper,nflverse,odds}`). A sync of
`raw` therefore means *ESPN's cache only* — `scripts/s3_sync.sh` excludes
the vendor subdirectories, which are addressed explicitly. Without that,
`espn.yml`'s `--delete` push would wipe the nflverse parquet mirror any time
it ran with a cold copy of it.

The Monday and Tuesday odds slots are also staggered 30 minutes behind
`espn.yml`'s, so they read an ESPN cache that has already been refreshed
rather than one being rewritten underneath them.

Outputs are re-keyed on the way in. `data/out/` uses the repo's
`dd-mm-yyyy-<name>.csv` convention; the archive stores them as
`archive/out/<dataset>/YYYY-MM-DD.csv` so a dataset's history sorts
lexically, which the `dd-mm-yyyy` form does not — the same reasoning
`sleeper/snapshots.py` gives for using ISO dates in the snapshot store.

Bucket versioning is enabled, which is the backstop for the one genuinely
dangerous operation here: a `--delete` sync of a corrupted local `state/`.

## Credentials

No AWS keys exist anywhere in GitHub. Workflows assume
`espn-ff-github-actions` via OIDC: GitHub mints a short-lived token, AWS
trades it for an hour-long session, and nothing long-lived is stored
*(Documented — GitHub OIDC)*.

The trust policy pins the token's `sub` claim with `StringEquals`, not a
`repo:…:*` wildcard. The claim is **not** the form most documentation
shows: this repository is issued *immutable subject claims*, so GitHub
embeds the numeric owner and repository ids —

```
repo:<owner>@<owner_id>/<repo>@<repo_id>:environment:gh_env
```

— rather than `repo:<owner>/<repo>:environment:<name>` *(Observed — read
off a real token; see `infra/README.md`)*. A policy written in the
plain-name form matches nothing, and the denial reads as
`Not authorized to perform sts:AssumeRoleWithWebIdentity`, which looks like
a missing permission rather than a failed condition. The id-bearing form is
also the stronger one: names can be renamed, transferred, deleted and
re-registered; ids cannot.

Two consequences worth stating plainly, because this repo is public:

- A fork's token carries its own owner and repository ids in `sub` and is
  rejected outright.
- **Every job that touches S3 must declare `environment: gh_env`.** Without
  it, both the secret lookup and the assume-role call fail. That failure is
  the control working.

The role's permissions stop at this one bucket, so even a successfully
assumed session can reach nothing else in the account.

`ESPN_S2`, `SWID`, `ODDS_API_KEY` and `AWS_ROLE_ARN` are `gh_env` secrets;
`AWS_REGION` and `S3_BUCKET` are `gh_env` variables. No `.env` is written
in CI — `config.load_dotenv` uses `os.environ.setdefault`, so workflow
`env:` entries win *(Documented — `espn_ff/config.py:60`)*.

## Exit codes

| Code | Meaning | CI treatment |
|---|---|---|
| 0 | Success | notice |
| 1 | Transient or unexpected failure | **failure** |
| 2 | The Odds credit guard declined to spend; nothing was issued | warning |
| 3 | ESPN session cookies expired | **failure — act now** |

Unattended runs are alerted on from the exit status alone, so the two
*predictable, actionable* failures get their own code instead of sharing 1
with every ESPN outage and network blip. Both subclass a broader error
(`BudgetExceeded` < `OddsError`, `PrivateLeagueError` < `EspnError`), so
the distinction lives entirely in `cli.py`'s handler ordering — which
`tests/test_cli_exit_codes.py` pins.

Exit 3 is the only one that always needs a person: see the runbook below.

## Runbook

### ESPN returns 401 AUTH_LEAGUE_NOT_VISIBLE

The `ESPN_S2`/`SWID` session cookies expired. This is the failure most
likely to happen and the one with no automated fix — they are browser
session cookies and cannot be refreshed programmatically.

Re-grab both from a logged-in browser (DevTools → Application → Cookies →
`fantasy.espn.com`), then:

```bash
./scripts/rotate_espn_cookies.sh
```

It sets both `gh_env` secrets and immediately dispatches `espn.yml`, waiting
for the verdict. Validating as part of rotating is the point: a truncated
`espn_s2`, or a `SWID` pasted without its braces, is indistinguishable from
a good value until something actually authenticates with it.

**There is no way to make GitHub run a workflow when a secret changes** —
Actions has no such event — so the rotation has to do it itself.

Nothing is lost while the cookies are dead, since ESPN's views are free to
re-fetch, *except* live Sunday scoring during the outage window, which
cannot be backfilled.

### How expiry gets noticed in the first place

`.github/workflows/health.yml` runs `probe` twice daily and again an hour
before Sunday kickoff. It touches no S3 and assumes no role — just the two
cookies — so a failure there means the credentials and not the
infrastructure. Exit 3 from it is the signal to rotate.

This is a schedule and not an event on purpose: the cookies die *between*
rotations, silently, and nothing changes at the moment they do. Triggering
on a secret change would only ever confirm the value you just pasted, never
catch the one quietly going stale.

### An odds job exited 2

The credit guard fired. Per `docs/odds-budget.md`'s runbook the message
says which budget was hit, and either way **nothing was issued** for
whatever the job did not reach.

Do not simply re-run it. Check the run receipt at
`logs/runs/odds/<run_id>/<slug>.json` (or `data/odds/last_run.json` after a
restore) for that job's `stale` flag and `reason` first — a budget-aborted
job leaves the prior snapshot in place, and a stale snapshot is
indistinguishable from a fresh one on disk. `captured_at` and `last_run.json`
are the truth; the parquet file's mtime is not.

### A workflow did not run at its scheduled time

Check the dead-letter queue first — that is what it is for. A message in
`ff-dispatch-dlq` means AWS could not deliver the dispatch, so the slot never
started, and the `ff-dispatch-failed` alarm should already have said so. The
commands are in `docs/aws-scheduling.md`.

An empty DLQ means the dispatch succeeded and the problem is downstream, on
GitHub's side. Every workflow keeps `workflow_dispatch`, so fire it by hand:

```bash
gh workflow run sleeper.yml
gh workflow run odds.yml -f job=pre_lock -f dry_run=false
```

Note the explicit `dry_run=false` for odds. That input defaults to **true**,
which is right for a human clicking "Run workflow" and wrong for a re-fire:
omit it and the job reports success having collected nothing. It emits a
`::warning::` when that happens rather than passing silently.

`pre_lock` sits at 10:38 ET, well before 13:00 ET kickoffs, so there is room
to notice a failure and re-fire before the lines lock.

### The bucket state looks wrong

Versioning is on. List versions of the object and restore the prior one
rather than re-running a job to regenerate it, which for odds data would
spend credits for a value that already exists.

## Known gaps

- **Expired ESPN cookies are unavoidable and unpredictable.** There is no
  API to refresh them and no advance warning before they lapse. This is the
  single biggest weakness in unattended operation. `health.yml` shortens the
  window between expiry and discovery to at most ~12 hours; it cannot
  prevent it, and a cookie that dies mid-Sunday still costs that afternoon's
  live scoring.
- **No cadence in this document has been Observed yet.** Every schedule
  here is as-configured, not as-measured — nothing has run on a real NFL
  week at the time of writing. Treat the timings as intent until a few
  weeks of `logs/runs/` exist to measure against.
- **Sunday's ESPN cadence is a guess bounded by an unknown.** ESPN
  publishes no rate limits, so every 30 minutes is chosen to be conservative
  rather than because any limit is known. If 429s appear, halve the frequency.
- **A dispatched run that then fails is not alarmed.** The DLQ alarm covers
  delivery, not execution. Nothing currently reads the run receipts under
  `logs/runs/`, so a workflow that starts and errors is only visible in
  GitHub's own run list.
- **The odds ledger is only serialized within GitHub.** The `odds-ledger`
  concurrency group prevents two CI runs from racing, but a manual local
  run during a scheduled one can still interleave. The blast radius is
  bounded — `spent_authoritative` re-establishes truth from the API's
  `x-requests-used` header on the next request — but the audit trail for
  the clobbered run is lost.
- **No lockfile.** `uv pip install -e ".[dev]"` resolves fresh each run, so
  an upstream release can break CI with no local change.
- **`latest/` duplicates `archive/`.** It exists for convenience; drop it
  if the redundancy stops earning its keep.
