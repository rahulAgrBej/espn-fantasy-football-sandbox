# Automation

This is the operational companion to
`docs/data-collection-weekly-schedule.md`, which says *what should be
pulled on each day of the week and why*. This document answers the
follow-on question: **how that schedule actually runs without a human, and
where the data goes.**

Five GitHub Actions workflows run the schedule; an S3 bucket holds both the
outputs and — the part that is easy to get wrong — the *state* those
workflows need in order to produce anything meaningful.

## How to read this

Cadence and behavioural claims below are tagged the same three ways as
`docs/data-sources.md`:

- **Observed** — measured from an artifact (a workflow run, an object in
  the bucket).
- **Documented** — asserted by a vendor, by GitHub's own docs, or by a
  docstring in this repo.
- **Inferred** — deduced from how the code behaves, not published anywhere.

## Overview

| Workflow | Schedule (UTC cron) | Local time | Runs | Metered |
|---|---|---|---|---|
| `sleeper.yml` | `0 12 * * *` | 08:00 ET daily | `sleeper`, `status` | no |
| `nflverse.yml` | `0 13,17,22 * * *` | 09/13/18 ET daily | `nflverse`, `features` | no |
| `nflverse.yml` | `0 13 * * 4` | Thu 09:00 ET | `nflverse --force`, `features` | no |
| `espn.yml` | `0 13 * * 1`, `0 13 * * 2` | Mon/Tue 09:00 ET | `pull --refresh`, `export --refresh` | no |
| `espn.yml` | `*/30 17-23 * * 0`, `*/30 0-4 * * 1` | Sun 13:00 ET – Mon 00:30 ET | same, every 30 min | no |
| `odds.yml` | five slots, see below | Tue/Thu/Fri/Sun/Mon | `odds <job>`, `projections` | **yes** |
| `health.yml` | `0 11 * * *`, `0 23 * * *`, `0 16 * * 0` | 07:00/19:00 ET, Sun 12:00 ET | `probe` | no |
| `tests.yml` | on push / PR | — | `pytest` | no |

The `odds.yml` slots map one-to-one onto the five-job schedule in
`docs/odds-budget.md`: `slate` (Tue 09:30 ET), `props` (Thu 10:00),
`line_movement` (Fri 10:00), `pre_lock` (Sun 10:30), `results` (Mon 09:30).
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
  logs/runs/  one receipt per workflow run
```

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
`logs/runs/odds/<run_id>.json` (or `data/odds/last_run.json` after a
restore) for that job's `stale` flag and `reason` first — a budget-aborted
job leaves the prior snapshot in place, and a stale snapshot is
indistinguishable from a fresh one on disk. `captured_at` and `last_run.json`
are the truth; the parquet file's mtime is not.

### A workflow did not run at its scheduled time

GitHub cron is best-effort: runs are routinely 5–30 minutes late and are
occasionally dropped entirely under load *(Documented — GitHub Actions)*.
Every workflow has `workflow_dispatch`; fire it by hand.

`pre_lock` is pinned at 14:30 UTC deliberately for this reason — 10:30 EDT,
09:30 EST — so even a delayed run lands well before 13:00 ET kickoffs.

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
- **Scheduled workflows are disabled after 60 days of repository
  inactivity** and silently re-enabled by any push *(Documented — GitHub)*.
  That window lands squarely in the offseason; check before Week 1.
- **No cadence in this document has been Observed yet.** Every schedule
  here is as-configured, not as-measured — nothing has run on a real NFL
  week at the time of writing. Treat the timings as intent until a few
  weeks of `logs/runs/` exist to measure against.
- **Sunday's ESPN cadence is a guess bounded by an unknown.** ESPN
  publishes no rate limits, so `*/30` is chosen to be conservative rather
  than because any limit is known. If 429s appear, halve the frequency.
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
