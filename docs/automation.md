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
| `espn.yml` | Mon 09:08, Tue 09:08, Wed 09:08 | `pull --refresh`, `export --refresh` | no |
| `espn.yml` | Sun 13:08 – Mon 00:38, every 30 min | same | no |
| `odds.yml` | five slots, see below | `odds <job>`, `projections` | **yes** |
| `health.yml` | 07:04 / 19:04 daily, Sun 12:04 | `probe` | no |
| `report.yml` | Mon 10:30 | `report --day monday` | no |
| `report.yml` | Tue 10:00 | `report --day tuesday` | no |
| `report.yml` | Tue 11:00 | `report --day tuesday-waivers` | no |
| `report.yml` | Wed 10:00 | `report --day wednesday` | no |
| `report.yml` | Thu 11:00 | `report --day thursday` | no |
| `report.yml` | Fri 11:00 | `report --day friday` | no |
| `report.yml` | Sat 10:00 | `report --day saturday` | no |
| `report.yml` | Sun 11:30 | `report --day sunday` | no |
| `summary.yml` | on `report` completing, **plus** eight backstop slots | `summarize` | no |
| `tests.yml` | on push / PR (GitHub's own trigger) | `pytest` | no |

`tests.yml` runs on `push` and `pull_request`; `summary.yml` runs on
`workflow_run` off `report`. Those are the only two rows GitHub triggers by
itself, and both are events rather than a clock — there is still no GitHub
cron anywhere in this repo. Every other row is an EventBridge schedule —
`docs/aws-scheduling.md` has the cron expressions, the inputs each one
sends, and the two 30-minute ordering gaps (espn→odds on Mon/Tue, nflverse
routine→forced on Thursday) that must survive any retiming.

`summary.yml` is the one workflow with two triggers. The `workflow_run`
event is the primary path and fires within seconds of a report landing in
S3; the eight EventBridge slots (Mon 10:50, Tue 10:20, Tue 11:20, Wed 10:20,
Thu 11:20, Fri 11:20, Sat 10:20, Sun 11:50) are a backstop for a dropped
event or a `report.yml` that never ran. Both
land in the same input-free, idempotent job, so a backstop firing after the
event already did the work finds nothing to do and spends nothing. See
`docs/ai-summaries.md`.

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
  summaries/  one JSON summary per report, append-only       (synced WITHOUT --delete)
  logs/runs/  one receipt per workflow run
```

`reports/` is a different kind of "exact mirror" than `state/`: it is safe
to sync with `--delete` not because one workflow owns it in the
one-writer-per-subtree sense, but because it mirrors a git-tracked
directory — `actions/checkout` restores the complete history before
`report.yml` runs, so the local copy is always the full, authoritative set
by the time it syncs.

**`summaries/` is the mirror image of that, and the difference is exactly
why it never takes `--delete`.** It is gitignored, so `actions/checkout`
restores none of its history and `summary.yml`'s local `summaries/` tree
holds only what that run produced. A `--delete` mirror from a tree that
partial would wipe every prior summary on every run. The prior history
reaches the runner as a read-only cache under `.cache/s3-summaries` instead,
which is never written to and never pushed — see `docs/ai-summaries.md`.

Three verbs serve it: `pull-reports` and `pull-summaries` fill those read
caches (neither uses `--delete`, so `sync-reports` stays the only
`--delete` path over `reports/`), and `sync-summaries` pushes the run's
output append-only.

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
| `report.yml` | `raw sleeper nflverse raw/nflverse odds`, plus `latest/out/{matchups,weekly-rosters,player-pool,roster-slots,teams,transactions}` | none of `data/` — see below |
| `summary.yml` | nothing under `data/` at all, only `latest/out/{teams,roster-slots,weekly-rosters}` plus the `reports/` and `summaries/` read caches | `summaries/`, which is outside `data/` and append-only |

`summary.yml` takes `report.yml`'s pattern one step further: it restores no
`data/` subtree whatsoever and so is not an `ff-run` caller at all (that
action's whole contract is restore → run → archive → push *state*). It is
also the only workflow that owns a bucket prefix outside `data/`, which is
why it gets `sync-summaries` rather than `push-state`.

The three exports it restores serve its two prompts: `teams` and
`roster-slots` fill the summary prompt's league-facts section, and
`weekly-rosters` is the grounded news layer's entire input. All three
degrade rather than failing — the first two render "unavailable in this run"
and a missing `weekly-rosters` becomes a stored `news_error` naming the
restore step, with the summary still written. **That degradation is silent
at the workflow level by design**, so a `restore-out` line that quietly
loses `weekly-rosters` costs every report its news while the workflow stays
green; `tests/test_workflow_contract.py` is the only thing that notices.

`report.yml` is the odd one out twice over: it is the first workflow that
passes an empty `push-paths` and means it (it restores several subtrees
read-only and owns none of them — `s3_sync.sh`'s `push-state` treats an
empty argument as "push nothing," not its old default of "push everything"),
and it also reads a slice of `data/out/` that `restore` never covers at all.
`data/out/` is purely an archive destination for every other workflow —
`report.yml` seeds it from `s3://$BUCKET/latest/out/<dataset>.csv` instead
(the "newest copy" convenience prefix from the bucket layout below), via a
new `s3_sync.sh restore-out` subcommand. `odds` is the one subtree in that
list `report.yml` restores that is owned by a *metered* workflow
(`odds.yml`) rather than a free one — still read-only and still pushed back
nowhere, but the read exists specifically so the Tuesday 11:00 waiver
report can price `implied_team_total` off the Tuesday `slate` job.

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

`ESPN_S2`, `SWID`, `ODDS_API_KEY`, `GEMINI_API_KEY` and `AWS_ROLE_ARN` are
`gh_env` secrets; `AWS_REGION` and `S3_BUCKET` are `gh_env` variables. No `.env` is written
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

**`summarize` is the one command that never returns anything but 0.** A
missing `GEMINI_API_KEY`, a missing report, a dead model or a dropped
connection all print to stderr and exit 0. That is a deliberate departure
from the table above, for this command only: a summary is additive — the
report is already rendered, committed and mirrored by the time it runs — so
nothing it can fail at is worth failing a run over. `GeminiError` is
correspondingly absent from `cli.main`'s except-chain, so the departure
cannot leak into any other command. `summary.yml` inverts it at the edge: a
non-zero exit there means something *outside* the command broke, and the
workflow does fail on it.

That holds for the grounded news layer too, one level down. A failed news
call does not fail the report it belongs to: the envelope is still written,
with `news: null` and a `news_error`, and the next trigger backfills only
the news. So a green `summary` run does **not** imply every envelope has
news — check `news_error` in the bucket, or the per-report line in the run
log, which names the search count or says `news FAILED`.

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

### Dispatching vs. running locally

**Dispatch, don't render locally.** `gh workflow run <name>.yml` is the
default way to produce any artifact this repo tracks, reports included:

```bash
gh workflow run espn.yml
gh workflow run report.yml -f day=tuesday
```

Every workflow restores its state from S3 and runs ESPN pulls with
`--refresh` before it does anything else *(Documented —
`.github/workflows/espn.yml:5-9`)*. A local clone does neither: its `data/`
tree reflects whatever it last happened to fetch, which goes stale the
moment a scheduled run lands elsewhere. `cmd_export`'s league-wide fetches
(`mSettings/mTeam/mStandings`, `mMatchupScore/mTeam`, and the rest listed in
`docs/data-sources.md`'s "Freshness mechanism") now carry a real 300-second
TTL (`ttl_for(None, current)`, `espn_ff/cache.py:75`), so a *long-idle*
local clone no longer serves an arbitrarily old snapshot forever — but a
clone that ran even one command in the last 5 minutes still reads its own
cache, and every other vendor's state (`sleeper`, `nflverse`, `odds`) has
no such TTL at all and depends entirely on the S3 restore below.

A local run is the debugging fallback, never the way a report or export
gets made.

**If you must run locally, sync first.** Restore state from S3 before any
`export`, `report`, or `features` run:

```bash
set -a; . .env; set +a
./scripts/s3_sync.sh restore "raw sleeper nflverse raw/nflverse odds"
./scripts/s3_sync.sh restore-out matchups weekly-rosters player-pool \
    roster-slots teams transactions
```

`.env` is gitignored and `s3_sync.sh` reads `S3_BUCKET`/`AWS_PROFILE`
straight from the environment, so it must be exported into the shell —
unlike `espn_ff/config.py`, which loads `.env` itself, sourcing here is not
automatic. See `.env.example` for both variables.

Then run `export --refresh` (never a bare `export`) before anything reads
its output — the 300s TTL above bounds the damage, but it doesn't guarantee
freshness at the moment you actually need it. And never commit an artifact
produced from unsynced local data over one a workflow already produced —
if a local render and a bot render disagree, the bot render is the one that
ran against fresh state.

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
- **`summary.yml`'s primary trigger is the one thing here GitHub delivers.**
  Everything else moved to EventBridge precisely because GitHub offers no
  delivery guarantee and no signal when a slot is skipped; a `workflow_run`
  event is a different reliability profile from GitHub cron, but it is not
  an independently observable one either. The eight AWS backstop slots bound
  the damage to ~20 minutes rather than eliminating it.
  `docs/ai-summaries.md` has the rest of that layer's gaps.
- **`latest/` duplicates `archive/`.** It exists for convenience; drop it
  if the redundancy stops earning its keep.
- **A report can succeed and say nothing.** The skip-on-missing discipline
  in `s3_sync.sh` is correct on the collection side — a missing dataset
  degrades a job rather than failing it, so a transient gap in one feed
  doesn't cascade into every workflow that reads state. On the
  consumption side, the same discipline converts a configuration error
  into a content error: `report.yml` restoring the wrong `state-paths` or
  `restore-out-datasets` list looks identical, at exit 0, to the upstream
  feed genuinely having nothing yet. `report-tuesday-waivers` hit exactly
  this during its own rollout, for an unrelated reason (a dedupe-key bug
  in `espn_ff/odds/store.py`, since fixed) — the run exited 0 and
  committed a report with every `implied_team_total` cell reading
  "insufficient data," which is far harder to notice than a red run. The
  same shape reappears from the local side rather than a `state-paths`
  misconfiguration: a local `report` run against a never-refreshed ESPN
  cache (see "Dispatching vs. running locally" above) rendered week 2's
  Tuesday reports with a 0-0 standings table and pre-kickoff matchup
  scores, both silently — the report guards fired correctly on genuinely
  stale input, and only a human reading the output noticed anything was
  wrong.
