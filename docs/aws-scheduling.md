# AWS scheduling

The clock that fires this repo's collection workflows lives in AWS, not in
GitHub. `docs/automation.md` covers how a run works once it starts and where
the data goes; this document covers **what starts it, why that moved off
GitHub, and how a failed trigger becomes visible.**

GitHub Actions still does all the work. Only the trigger moved.

## How to read this

Cadence and behavioural claims are tagged the same three ways as
`docs/data-sources.md`:

- **Observed** — measured from an artifact (a workflow run, a CloudWatch
  metric, an object in the bucket).
- **Documented** — asserted by a vendor or by a docstring in this repo.
- **Inferred** — deduced from how the code behaves, not published anywhere.

## Why the clock moved

GitHub's `on.schedule` cron is best-effort and offers no delivery guarantee,
no retry, no dead-letter, and no signal when a slot is skipped *(Documented —
GitHub Actions)*. In this repository it did not merely run late, it failed to
run at all:

- Between workflow registration at 04:08:46Z and 14:40Z on 2026-09-15, **five
  due slots produced zero runs**. Every run in the repo's history to that
  point was `push`, `pull_request` or `workflow_dispatch`; `gh run list
  --event schedule` returned an empty list *(Observed)*.
- The `health` 11:00Z slot was eventually delivered at **15:30:10Z — 4h30m
  late** *(Observed)*. A "check the cookies before the day's collection" probe
  that lands four and a half hours late has already missed the thing it exists
  to protect.

Moving every cron off `:00`/`:30` was the first mitigation, on the theory that
top-of-hour congestion was the cause. It reduces the odds of a drop but does
not address the real problem: **nothing outside GitHub knew a slot was due**,
so a miss stayed silent indefinitely.

That is unacceptable for three feeds whose data cannot be recovered:

| Feed | Why a missed slot is permanent |
|---|---|
| `sleeper` daily | `practice_trajectory` is reconstructed from our own consecutive snapshots; Sleeper publishes no history *(Documented — `espn_ff/sleeper/snapshots.py`)*. |
| `espn` Sunday live scoring | In-progress scores cannot be re-fetched once a game ends. |
| `odds pre_lock` | The Odds API has no historical endpoint *(Documented — `docs/odds-budget.md`)*. |

EventBridge Scheduler fires at the exact minute, retries, dead-letters what it
cannot deliver, and raises a CloudWatch alarm when it does. That last property
is the one GitHub never had.

## The path

EventBridge Scheduler cannot call an HTTPS endpoint directly — it supports only
templated AWS targets and universal (AWS SDK) targets *(Documented — AWS)*. But
`PutEvents` **is** a templated target and an event-bus rule **can** target an
API destination, so the following reaches GitHub with no Lambda anywhere:

```
Schedule (America/New_York, FlexibleTimeWindow=OFF)
   └─ PutEvents ─► ff-dispatch bus ─► rule (one per workflow)
                                        └─ API destination
                                           POST /repos/{owner}/{repo}
                                                /actions/workflows/{file}/dispatches
                                           └─► GitHub Actions run
   failures ─────────────────────────────────► ff-dispatch-dlq ─► alarm
```

Everything above is one CloudFormation stack, `infra/scheduler.yaml`.

There is one rule per workflow rather than one rule total because the dispatch
URL differs per workflow and a target's `PathParameterValues` is static. The
`espn`, `sleeper` and `health` rules send a constant body; the `nflverse`,
`odds` and `report` rules assemble theirs with an input transformer from
fields the schedule puts in the event detail.

## The schedules

All seventeen are pinned to `America/New_York`. EventBridge cron takes six fields
— minute, hour, day-of-month, month, day-of-week, year — and requires `?` in
one of the two day fields.

| Schedule | Expression | ET slot | Inputs sent |
|---|---|---|---|
| `health-morning` | `cron(4 7 * * ? *)` | 07:04 daily | — |
| `health-evening` | `cron(4 19 * * ? *)` | 19:04 daily | — |
| `health-sunday` | `cron(4 12 ? * SUN *)` | Sun 12:04 | — |
| `sleeper-daily` | `cron(11 8 * * ? *)` | 08:11 daily | — |
| `espn-monday` | `cron(8 9 ? * MON *)` | Mon 09:08 | — |
| `espn-tuesday` | `cron(8 9 ? * TUE *)` | Tue 09:08 | — |
| `espn-sunday-live` | `cron(8,38 13-23 ? * SUN *)` | Sun 13:08–23:38 | — |
| `espn-monday-night` | `cron(8,38 0 ? * MON *)` | Mon 00:08, 00:38 | — |
| `nflverse-routine` | `cron(23 9,13,18 * * ? *)` | 09:23 / 13:23 / 18:23 | `force=false` |
| `nflverse-thursday` | `cron(53 9 ? * THU *)` | Thu 09:53 | `force=true` |
| `odds-slate` | `cron(38 9 ? * TUE *)` | Tue 09:38 | `job=slate`, `dry_run=false` |
| `odds-props` | `cron(8 10 ? * THU *)` | Thu 10:08 | `job=props`, `dry_run=false` |
| `odds-line-movement` | `cron(8 10 ? * FRI *)` | Fri 10:08 | `job=line_movement`, `dry_run=false` |
| `odds-pre-lock` | `cron(38 10 ? * SUN *)` | Sun 10:38 | `job=pre_lock`, `dry_run=false` |
| `odds-results` | `cron(38 9 ? * MON *)` | Mon 09:38 | `job=results`, `dry_run=false` |
| `report-monday` | `cron(30 10 ? * MON *)` | Mon 10:30 | `day=monday` |
| `report-tuesday` | `cron(0 10 ? * TUE *)` | Tue 10:00 | `day=tuesday` |

### DST stops mattering

Under the old UTC crons every slot silently shifted an hour on the first Sunday
in November, and the workflow comments had to carry notes like "10:38 EDT /
09:38 EST". A named timezone holds the ET wall clock: `odds-pre-lock` fires at
10:38 ET on 2026-10-11 and at 10:38 ET on 2026-11-15, and it is the UTC instant
that moves instead *(Observed — evaluated across the 2026-11-01 boundary before
deploying)*.

The ET-native form also simplified the Sunday grid. In UTC it needed two
expressions because the block crosses midnight UTC (`8,38 17-23 * * 0` plus
`8,38 0-4 * * 1`); in ET it is one continuous evening plus a single midnight
hour, with identical coverage — 24 fires from Sun 13:08 through Mon 00:38.

### Four gaps that must survive any retiming

1. **espn `:08` → odds `:38` on Mon/Tue.** The odds jobs read the ESPN cache
   that `espn.yml` owns, so they must go second.
2. **nflverse `:23` routine → `:53` forced on Thursday.** The forced canonical
   read has to land last to be the one that survives.
3. **espn `:08` and nflverse `:23` → report `:30` on Monday.** The Monday
   report reads both the ESPN export and nflverse's routine injury pull, so
   it must go after both. Tuesday's analogous gap is simpler: espn `:08` →
   report `:00` on Tuesday, with no nflverse dependency, since Tuesday's
   report never reads nflverse.
4. **`odds` never auto-retries** — see below.

## Retry policy is deliberately not uniform

EventBridge delivers at least once *(Documented — AWS)*. A `5xx` returned
*after* GitHub has already created the run would therefore dispatch a
duplicate. For every workflow except `odds` that is harmless: the commands are
idempotent and the concurrency group serializes them.

For `odds` it is not. A duplicate `pre_lock` can spend another 25 credits
against a 500-credit period, and credits do not come back. So the `odds` rule
and the five `odds` schedules set `MaximumRetryAttempts: 0` and rely on the DLQ
alarm instead: a missed odds slot that a person re-fires by hand is strictly
cheaper than a double-spend.

## How a failed trigger becomes visible

This is the part that did not exist before.

EventBridge retries `401/407/409/429/5xx` and does **not** retry other `4xx`
*(Documented — AWS)*. An expired or revoked token answers `403`, so it lands in
`ff-dispatch-dlq` on the first attempt rather than being absorbed. Any message
in that queue trips the `ff-dispatch-failed` CloudWatch alarm.

**A message in the DLQ means the slot did not run.** Read it to find out which:

```bash
aws sqs receive-message --queue-url "$(aws cloudformation describe-stacks \
  --stack-name ff-scheduler \
  --query 'Stacks[0].Outputs[?OutputKey==`DlqUrl`].OutputValue' --output text)"
```

Then fix the cause and re-fire the slot by hand — `gh workflow run odds.yml -f
job=pre_lock -f dry_run=false`, or put the event back on the bus.

Note what the alarm does *not* cover: it fires when the **dispatch** fails, not
when the run fails. A workflow that starts and then errors is GitHub's own
failure surface, and the run receipts under `logs/runs/` remain the record of
what actually executed.

## Credentials

### The dispatch token

A GitHub fine-grained PAT, repository-scoped to this repo, with **Actions: read
and write** and nothing else — notably *not* `Contents`, so a leaked token
cannot push code. It lives in the EventBridge connection `ff-github-dispatch`,
which stores it in Secrets Manager through EventBridge's service-linked role.

Rotate it with `./scripts/rotate_dispatch_token.sh`, which checks the token
against GitHub directly, updates the connection, waits for it to leave
`AUTHORIZING`, then dispatches `health.yml` through the whole AWS path to prove
it works. Rotation deliberately does **not** go through CloudFormation: a stack
update would put a live credential through stack parameters every time. The
cost is that the stack shows permanent drift on that one property.

**Current token expires: 2027-02-28.** Issued 2026-09-15. Fine-grained PATs are
capped at 366 days *(Documented — GitHub)*; this one is shorter than the cap, so
it lapses mid-offseason rather than during a season — deliberate, since a
dispatch credential dying in September would cost live-scoring data that cannot
be backfilled. Rotate it before Week 1 of the 2027 season regardless of how much
life is left on it.

### IAM

Two roles, both created by the stack, both conditioned on `aws:SourceAccount`
— without that condition either is assumable on behalf of any AWS account.

| Role | Trusts | May |
|---|---|---|
| `espn-ff-scheduler-execution` | `scheduler.amazonaws.com` | `events:PutEvents` on the `ff-dispatch` bus |
| `espn-ff-dispatch-invoke` | `events.amazonaws.com` | `events:InvokeApiDestination` on the destination |

**The OIDC trust policy is untouched.** `infra/trust-policy.json` pins `sub` to
`repo:<OWNER>@<ID>/<REPO>@<ID>:environment:gh_env`. That claim encodes the
environment, not the triggering event, so a `workflow_dispatch`-triggered job
that declares `environment: gh_env` produces a byte-identical subject. Nothing
about S3 access changed.

## Deploying

```bash
aws cloudformation deploy \
  --template-file infra/scheduler.yaml \
  --stack-name ff-scheduler \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
      GitHubToken="$GH_DISPATCH_TOKEN" \
      AlarmEmail=you@example.com \
      HealthScheduleState=ENABLED
```

Every schedule defaults to `DISABLED`. The rollout enables them one workflow at
a time, and the order within each step matters: **land the `on.schedule`
removal on the default branch first, then flip the parameter.** `on.schedule`
stops applying the moment it is off `main`, so doing it the other way round
leaves a window where GitHub and AWS both own the slot.

Unlike `infra/*.json`, this template carries no account id and is tracked in
git — `.gitignore` and `.githooks/pre-commit` block the rendered JSON policies
but not this.

### Testing without waiting for a slot

Put an event straight on the bus. This exercises rule → transformer →
destination → auth in one shot:

```bash
aws events put-events --entries '[{"EventBusName":"ff-dispatch",
  "Source":"ff.scheduler","DetailType":"dispatch.health","Detail":"{}"}]'
gh run list --workflow health.yml --event workflow_dispatch --limit 3
```

Worth doing the negative case too, because an alarm nobody has seen fire is an
assumption: dispatch to a workflow filename that does not exist, confirm GitHub
answers `404`, and confirm the message reaches the DLQ and trips the alarm.

## What this closed

GitHub disables scheduled workflows after 60 days of repository inactivity
*(Documented — GitHub)*, a gap `docs/automation.md` listed as unaddressed with
the window landing in the offseason. With no `on.schedule` left in the repo
there is nothing to disable, and `workflow_dispatch` has no equivalent
auto-disable.

## Known gaps

- **The dispatch token expires 2027-02-28**, which introduces a second
  credential that dies on a clock — the same class of problem as the ESPN
  cookies. The date lands in the offseason, which is the best case, but nothing
  in this repo watches it: the DLQ alarm only fires *after* the first dispatch
  has already failed, and GitHub's expiry email goes to a person, not to the
  pipeline. The documented upgrade is a GitHub App plus a small Lambda that
  mints a one-hour installation token per call, removing expiry entirely; it
  was deferred to keep this migration Lambda-free.
- **At-least-once delivery is mitigated for `odds`, accepted elsewhere.** A
  fully idempotent design would gate dispatch on the S3 receipts under
  `logs/runs/`, which is more machinery than the risk currently justifies.
- **Nothing has been Observed on a real NFL week.** The DST arithmetic and the
  ordering gaps were verified before deploying, but no full week has run
  through this path. Treat the timings as intent until `logs/runs/` has a few
  weeks to measure against.
- **`report-monday` has never fired on a real week.** It is also the first
  schedule whose dispatched workflow (`report.yml`) writes back to the repo
  rather than only to S3 — a class of failure (a lost git-push race) that
  the DLQ/alarm here does not and cannot cover, since that alarm only
  watches dispatch delivery, not what the workflow does once it starts. See
  `docs/report-weekly-schedule.md`'s known gaps for what the report itself
  cannot see. `report-tuesday` is newly added and has never fired at all
  yet, sharing the same un-Observed caveat and the same git-push-race blind
  spot.
- **Sunday has never been exercised at all.** The ESPN live-scoring grid, the
  Sunday `health` probe and `odds pre_lock` have never fired once, under either
  scheduler. The first Sunday after cutover should be watched live.
- **The alarm covers dispatch, not execution.** A slot that dispatches cleanly
  and then fails inside GitHub does not touch the DLQ. Detecting *that* means
  reading the run receipts, which nothing currently does.
- **Compute stays on GitHub — assessed, not moved.** Three things make a Lambda
  port larger than it looks: `espn_ff/config.py` derives `PROJECT_ROOT` from
  `__file__` with no env override, so every path assumes a writable repo tree
  while Lambda's `/var/task` is read-only; runtime dependencies are ~240 MB
  (pyarrow, pandas, duckdb, numpy) against a 250 MB unzipped limit, forcing a
  container image plus a build pipeline that would itself run on GitHub; and
  the credit ledger's `BEGIN IMMEDIATE` safety comes from GitHub's
  `odds-ledger` concurrency group, for which Lambda has no equivalent. Nothing
  runs anywhere near the 15-minute limit, so duration is not the blocker — the
  filesystem and the serialization guarantee are. The repo is public, so runner
  minutes are free and unlimited. **If compute ever does move, the destination
  is ECS Fargate `RunTask`, not Lambda**: it is a native Scheduler templated
  target, has no 15-minute cap, and gives a writable filesystem, which makes
  the `config.py` refactor optional rather than mandatory.
- **The pre-existing AWS resources are still not in any template.** The OIDC
  provider, the `espn-ff-github-actions` role and the bucket were created by
  hand and remain undocumented (`infra/README.md` records only the
  `update-assume-role-policy` call). This stack sits alongside them rather than
  adopting them.
