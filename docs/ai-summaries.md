# AI summaries

The four weekly reports are dense — week 2's waiver report is 166 lines with
seven per-slot candidate tables — and none of them says *what changed since
last week* or *what the one decision actually is*. Reading them is the work.
This layer generates a short prose summary of each one and stores it as its
own JSON object under the bucket's `summaries/` prefix.

The report markdown is **never modified**. A summary is a separate artifact
with a separate lifecycle, so a dead, rate-limited or unconfigured model can
never corrupt or block the report it describes. Everything below follows
from that one decision.

## How to read this

Cadence and behavioural claims are tagged the same three ways as
`docs/data-sources.md` and `docs/automation.md`: **Observed** (measured from
an artifact), **Documented** (asserted by a vendor or by a docstring in this
repo), **Inferred** (deduced from how the code behaves, not published
anywhere).

## Overview

| Piece | Where | What it does |
|---|---|---|
| `espn_ff summarize` | `espn_ff/cli.py` | Summarizes every report with no summary yet. Takes no report input. Always exits 0. |
| `espn_ff/ai/client.py` | — | `requests`-based Gemini REST client; key in a header, never a URL |
| `espn_ff/ai/prompt.py` | — | Pure prompt assembly — no disk, no network, no clock |
| `espn_ff/ai/reports.py` | — | Discovery, prior selection, header parsing |
| `espn_ff/ai/summarize.py` | — | Orchestration and the JSON envelope |
| `summary.yml` | `.github/workflows/` | `workflow_run` off `report`, plus dispatch |
| `SummaryRule` + 4 schedules | `infra/scheduler.yaml` | The AWS backstop, 20 min behind each report slot |
| `summaries/` | the bucket | One JSON envelope per summarized report, append-only |

Cost is roughly **$0.03 per summary, $0.12 a week** at four summaries
*(Observed — two real generations on 2026-09-15: 27,313 and 25,529 prompt
tokens, 292 and 253 answer tokens, plus 1,906 and 1,742 **thinking** tokens,
against introductory $0.75/1M in and $3.75/1M out)*. Not credit-metered, so
unlike `docs/odds-budget.md`'s ledger there is no guard, no quota and
nothing to reconcile. The number is stated here anyway because this repo
tracks metered spend deliberately and a reader should be able to tell the
two situations apart.

**Thinking tokens are about a quarter of the cost and all of the risk.**
This model reasons before it writes, those tokens are spent against
`maxOutputTokens` and billed at the output rate, and they outnumber the
answer roughly 6:1. `client.MAX_OUTPUT_TOKENS` is therefore sized for
thinking, not for prose — see "The first deployed run failed" below.

## The command takes no input, and that is the design

`summary.yml`'s primary trigger is `workflow_run` off `report.yml`. That is
the natural "run after the report landed" signal: report.yml's **last** step
is `Mirror reports/ to S3`, so by the time the run completes the markdown is
already in the bucket. It costs four lines of YAML and no new AWS
resources.

The catch is that `github.event.workflow_run` does **not** carry the
triggering run's `workflow_dispatch` inputs *(Documented — GitHub)*, so
`summary.yml` cannot learn which day was rendered. Every way of plumbing it
across is fragile — an artifact, a marker object inside a `--delete`-mirrored
prefix, scraping the commit message. So the command does not need it:

> **`espn_ff summarize` summarizes every report on S3 that has no
> corresponding summary object yet.**

That is strictly better than plumbing the day through. It makes the command
**idempotent**, so the event trigger and the scheduled backstop can both
fire without double-summarizing; and it makes it **self-healing**, so a
summary missed because the model was down gets picked up by the next trigger
instead of being lost. `--day` / `--week` / `--force` remain as filters for
debugging and backfill, and the scheduled path passes none of them.

"Has a summary" is checked against **both** the pulled cache of everything
already on S3 *and* the output tree this run is writing into. Checking only
the first would make idempotency depend on the S3 push having succeeded —
two runs before a push would summarize the same report twice.

## S3 is the source of truth; the checkout is a backup

Every input this command reads comes from the bucket first:

| Input | Primary (S3) | Backup |
|---|---|---|
| Rendered reports | `reports/` → `.cache/s3-reports` | the git checkout's `reports/`, only when the mirror is empty |
| Existing summaries | `summaries/` → `.cache/s3-summaries` | none — `summaries/` is gitignored, so no local history exists |
| `teams` / `roster-slots` | `latest/out/` → `data/out/` | none — degrades to "unavailable in this run" |

The reports fallback is a **strict fallback, never a merge**
(`ai/summarize.py:resolve_source`). Merging would let a report that exists
only in the checkout be summarized against a `report.path` naming a bucket
object that does not exist — the envelope would point at nothing. So either
every report a run sees came from S3, or none did.

Which one it was is recorded in every envelope as `report.source`
(`"s3"` or `"local"`), and a fallback prints two `[warning]` lines naming
the empty mirror and telling the reader to check that report.yml's
`Mirror reports/ to S3` step is landing. A bucket that is empty while the
checkout has reports means the mirror is broken, and a silent fallback would
make that look like a healthy pipeline for as long as nobody checked.

A failed *pull* is a different case and needs no fallback: `s3_sync.sh` runs
under `set -e`, so an unreachable bucket fails the workflow step before
`summarize` ever runs. An empty cache therefore means "the prefix is empty",
not "the pull broke".

If the **summaries** pull returns nothing when the bucket does have
summaries, the run re-summarizes up to `--limit` reports and overwrites them
on push. That costs a few cents and produces correct output, which is why
there is no backup on that row.

## Triggering: event first, schedule as backstop

Both paths land in the same job.

| Path | Mechanism | Purpose |
|---|---|---|
| Primary | `on: workflow_run: {workflows: [report], types: [completed]}` | Fires within seconds of the report landing in S3 |
| Backstop | Four EventBridge schedules → `dispatch.summary` → `summary.yml` | Catches a dropped event, or a `report.yml` that never ran |

The backstop is in character for this repo. `docs/aws-scheduling.md` records
why the clock moved to AWS at all: five consecutive missed GitHub-cron slots
on 2026-09-15, one landing 4h30m late, and nothing outside GitHub knowing a
slot was due. A GitHub-internal event is a different reliability profile
from GitHub cron, but it is not an independently observable one either — so
the AWS schedule stays as the thing that notices.

The four backstops sit 20 minutes behind each report slot: Mon 10:50, Tue
10:20, Tue 11:20, Wed 10:20 ET. Because the job is input-free all four send
an identical, empty payload; four exist rather than one so Tuesday's waiver
summary lands on Tuesday rather than waiting for the next slot to come
round.

`SummaryRule` keeps the default `MaximumRetryAttempts: 5`, unlike the five
`odds` schedules which deliberately set 0. EventBridge delivers at least
once *(Documented — AWS)*, and a duplicate delivery here finds every report
already summarized and spends nothing — the opposite of a duplicate
`pre_lock`, which spends 25 credits that do not come back.

Two details about `workflow_run` worth stating, because both are the kind of
thing that fails silently:

- It only fires for a workflow file present on the **default branch**
  *(Documented — GitHub)*. `summary.yml` is on `main`, so this holds.
- It matches on the triggering workflow's **name**, not its filename. A
  rename of `report.yml`'s `name:` would stop the event firing with no error
  anywhere — summaries would simply arrive 20 minutes late forever, off the
  backstop. `tests/test_workflow_contract.py` is the only thing that
  notices.

The job is gated on the triggering run's conclusion, since `workflow_run`
fires on failure and cancellation too. The dispatch path carries no
`workflow_run` context at all, hence the `event_name` arm:

```yaml
if: >-
  github.repository == 'rahulAgrBej/espn-fantasy-football-sandbox' &&
  (github.event_name != 'workflow_run' ||
   github.event.workflow_run.conclusion == 'success')
```

`report.yml` itself is **unchanged**, and the GITHUB_TOKEN "does not trigger
further workflows" restriction does not apply here: report.yml's runs are
started by `workflow_dispatch` through EventBridge's PAT, and a
dispatch-triggered run fires `workflow_run` normally.

## What the model is told

The system instruction is ~87k characters, ~22k tokens *(Observed — measured
by `build_prompt` against the two reports in `reports/2026/week-02/`; the
token figure Inferred at ~4 chars/token)*. It grows with the two docs it
embeds, so that number tracks them rather than being fixed.

Most of it is material that **already exists in this repo**, passed through
verbatim so it tracks the docs as they change rather than drifting into a
second, staler copy:

1. **League facts** — our team name and the legal lineup shape, from
   `latest_export("teams")` and `latest_export("roster-slots")`. Either
   missing renders an explicit "unavailable in this run" line rather than
   being omitted, on the same reasoning as `render.header_lines`' week line:
   a missing line is indistinguishable from a league with no teams.
2. **Column dictionary** — `docs/data-sources.md` verbatim. It is already a
   field-level dictionary with one row per column and a `Meaning` cell, and
   it carries the traps next to the meanings (`points_final` is `0.0` while
   the week is open, `winner` reads `UNDECIDED`, `pos_rank` is not
   week-aligned).
3. **Report semantics** — `docs/report-weekly-schedule.md` verbatim: what
   each report is for and what decision is due.
4. **Derived columns** — `prompt.DERIVED_COLUMNS`, the one hand-written
   part. These appear as table headers in the rendered reports and are
   defined nowhere under `docs/`, only in the docstrings of the modules that
   compute them, so without this section the model invents a definition for
   each one.
5. **House rules** — `prompt.HOUSE_RULES`, so the summary inherits this
   repo's epistemics.
6. **Output contract** — `prompt.OUTPUT_CONTRACT`: plain prose, no heading,
   no tables, a hard word cap, lead with the decision due, close by naming
   what the report cannot see.

The user message is today's report in full, then the last four prior
same-type reports oldest-first, each labelled with its week and render date.
Priors never cross a season boundary — last season's report compares this
week against a roster, a league and a scoring system that no longer exist,
which reads as a change when it is a different league-year entirely. Week 1
has zero priors and the message says so outright rather than leaving the
model to explain the absence as a data problem.

### Why section 4 has to exist

Two columns in these reports are both called `gap` and mean different
things. In the waiver add-candidate blocks it is a free agent's projection
minus the weakest eligible bench player's — the points gained by adding the
candidate *and benching the floor*, not by adding them outright. In the
Tuesday regret table it is a retrospective margin over an independent
counterfactual. One definition covering both is wrong twice, and neither is
written down outside `waivers.py` and `tuesday.py`.

The same applies to `ROS projection` (Inferred in `tuesday.py` as
`season_projected - season_points`; player-pool.csv publishes no such
column), `tier`'s deliberate sixth `UNKNOWN` value, the three-source
availability precedence, the free-agent anti-join, `week_projection`'s
`pool`/`roster` source label, and `render.py`'s three distinct empty states
(`--`, `_(none)_`, and the literal string `insufficient data`).

### The house rules that are load-bearing

Most are ordinary care. Three are not:

- **Never sum per-slot regret gaps.** Each regret row is an independent
  counterfactual over the same bench, so the same player can answer several
  rows and the column does not add. `optimal_lineup` is the one number in
  these reports that legitimately aggregates the week.
- **Never substitute a number where the report says `insufficient data`.**
  That string is a deliberate refusal to print a figure. Filling it from a
  prior week, a neighbouring row, or the model's own knowledge of the player
  converts a stated blind spot into a confident wrong answer.
- **Retrospective regret is not a start/sit rule.** "Player X outscored your
  starter last week" describes last week. Converting it into a lineup
  recommendation is the single most plausible-sounding mistake available
  here.

## The envelope

One object per summarized report, at
`summaries/<season>/week-NN/<YYYY-MM-DD>-<day_label>-<slug>.json` — the
report tree's own shape, one prefix over.

```json
{
  "schema_version": 1,
  "season": 2026, "week": 2, "day": "tuesday-waivers",
  "report": {
    "path": "reports/2026/week-02/2026-09-15-tuesday-waiver-wire.md",
    "title": "Waiver wire and opening market -- 2026 week 2",
    "covers": "week 2's waiver window; settlements also span week 1 (...)",
    "week_window": "Tue 2026-09-15 03:00 - Tue 2026-09-22 03:00 ET",
    "rendered": "Tue 2026-09-15 21:18 ET",
    "source": "s3",
    "sha256": "<64 hex chars of the report text>"
  },
  "summary_markdown": "...",
  "model": "gemini-3.8-flash",
  "generated_at": "2026-09-15T21:20:11-04:00",
  "prior_reports": ["reports/2026/week-01/....md"],
  "prompt_sha256": "<64 hex chars of system instruction + user message>",
  "usage": {
    "promptTokenCount": 27313, "candidatesTokenCount": 292,
    "thoughtsTokenCount": 1906, "cachedContentTokenCount": 0,
    "totalTokenCount": 29511
  }
}
```

`report.sha256` and `prompt_sha256` are the point of the envelope. They are
what lets a later reader tell whether a summary still describes the report
sitting next to it, and whether the prompt that produced it is still the one
in the tree — this repo's "freshness from the artifact, never from mtime"
rule applied to a new artifact. `report.path` names the **bucket key**, not
the runner cache path the file was actually read from, because a path under
`.cache/s3-reports` is meaningless once the runner is gone.

The four header fields are **parsed** off the 5-line block
`espn_ff/report/render.py:header_lines` emits, not re-derived — so they
describe the artifact on disk even if a later render would produce something
different. `tests/test_ai_reports.py` round-trips that contract between the
two modules, which do not import each other.

`usage` is what turned the cost figures above from Inferred into Observed,
and `thoughtsTokenCount` specifically is what makes a MAX_TOKENS failure
diagnosable from the stored artifact rather than only from a live retry.

## The first deployed run failed, and that is the story

The first real dispatch (run `35051048341`, 2026-09-16) exited 0, wrote no
envelope, and logged this for both reports:

```
[warning] 2026-09-15-tuesday-waiver-wire: generation stopped with
finishReason=MAX_TOKENS -- refusing to return a truncated or filtered
summary -- left unsummarized for the next run
```

`MAX_OUTPUT_TOKENS` had been set to 700 — roughly twice the
`OUTPUT_CONTRACT` word cap, which is the right size for the *answer* and
nowhere near enough for this model. An Observed generation spends ~1,900
thinking tokens to produce a ~290-token, ~180-word summary. The budget is
shared, so every call tripped the ceiling mid-thought.

Three things went right, and they are the reason this is a footnote rather
than an incident:

- **Nothing was stored.** The `finishReason != STOP` check refused the
  truncated output. A summary that stopped mid-sentence would have read as
  complete and sat in the bucket indefinitely.
- **The run still exited 0** and pushed nothing, so no report was blocked and
  no workflow went red for a failure that costs a summary.
- **It was self-healing by construction.** Both reports stayed unsummarized,
  so the next trigger picked them up with no backfill, no `--force`, and no
  manual step.

The fix was `MAX_OUTPUT_TOKENS = 4000` plus recording `thoughtsTokenCount`
in every envelope, so the next person sees the real number instead of
inferring it. The `MAX_TOKENS` error message now names thinking, the answer
size and the cap, because the instinctive fix — turning down the prose word
cap — would have changed nothing.

A remaining lever, deliberately not pulled: `generationConfig.thinkingConfig`
could bound thinking explicitly rather than leaving it to the model's
default. Adding an untested field to a working request was the worse trade
once the cap was sized correctly.

## Why it always exits 0

`cmd_summarize` returns `EXIT_OK` for every failure it knows how to have: a
missing `GEMINI_API_KEY`, a missing report, a `GeminiError`, a dead
connection. All of them print to stderr and return 0.

This is a deliberate departure from this CLI's "1 = transient failure"
convention, for this one command. A summary is additive — the report is
already rendered, committed and mirrored by the time this runs — so nothing
this command can fail at is worth failing a run over. `GeminiError` is
correspondingly **absent** from `cli.main`'s except-chain; the command
catches it itself, so the departure cannot leak into any other command.

Failure is also per report, not per run: a model that dies on the third of
four reports still leaves the first two on disk, and writes no envelope for
the third, so the next trigger retries exactly that one.

`summary.yml` inverts this at the edge: because the command is built to exit
0 on everything it anticipates, a **non-zero** exit means something outside
the command broke, and the workflow does fail on it.

## Storage and the two directories

Three new `s3_sync.sh` verbs:

| Verb | Direction | `--delete`? |
|---|---|---|
| `pull-reports` | `s3://.../reports` → `.cache/s3-reports` | no — a read cache |
| `pull-summaries` | `s3://.../summaries` → `.cache/s3-summaries` | no — a read cache |
| `sync-summaries` | `summaries/` → `s3://.../summaries` | **no — see below** |

Neither pull uses `--delete`, which keeps `cmd_sync_reports` the only
`--delete` path over the `reports/` prefix and leaves its header comment
about why that is safe there as the whole story.

`sync-summaries` not using `--delete` is load-bearing. Unlike `reports/`,
`summaries/` is gitignored, so `actions/checkout` restores none of its
history and the local tree holds **only what this run produced**. A
`--delete` mirror from a tree that partial would wipe every prior summary in
the bucket on every run — precisely the `state/` vs `archive/` distinction
`s3_sync.sh`'s header spells out, with `summaries/` firmly on the `archive/`
side.

That is also why the read cache and the output tree are separate
directories: `.cache/s3-summaries` holds the full history and is only ever
read; `summaries/` holds one run's output and is only ever pushed.

No IAM change was needed — the existing policy grants
`Get/Put/DeleteObject` on `<BUCKET>/*` and `ListBucket` on the bucket — and
no lifecycle rule touches the new prefix; the existing three are scoped to
`archive/`, `state/`, and incomplete multipart uploads.

## Credentials

`GEMINI_API_KEY` is a `gh_env` secret, alongside `ESPN_S2`, `SWID`,
`ODDS_API_KEY` and `AWS_ROLE_ARN`. `summary.yml` masks it with
`::add-mask::` the way `ff-run` does for the odds key.

The key is sent as an **`x-goog-api-key` header**, never as the `?key=`
query parameter the vendor's quickstart uses. `espn_ff/odds/client.py`
carries a whole `_redact` layer solely because The Odds API forces its key
into the URL; keeping it out of the URL here means there is no URL,
exception or log line it can leak through in the first place, so no
redaction layer exists and none is needed. `tests/test_ai_client.py` pins
that across every error path.

`.githooks/pre-commit` scans staged additions for `GEMINI_API_KEY=<value>`
on the same pattern it already used for `ODDS_API_KEY`.

## Why this workflow is not an `ff-run` wrapper

`ff-run`'s contract is "restore state → run → archive → push state →
receipt". This job touches no `data/` state, needs a credential `ff-run` does
not carry, and has to interleave three S3 reads before the Python run.
Writing the steps out keeps `ff-run` unchanged for its five existing
callers. It still writes a receipt under `logs/runs/summary/<run_id>/`, so it
is not the one job invisible to the operational trail.

## Verifying a change to this layer

Offline, no API key, no network:

```bash
pytest -m "not integration and not odds_live" -q

# Build the real prompts against the reports in the tree and print sizes.
python - <<'PY'
from pathlib import Path
from espn_ff.ai import reports, summarize
all_r = reports.scan("reports")
for r in all_r:
    s, u = summarize.build_prompt(r, reports.priors(all_r, r), Path("docs"))
    print(f"{r.stem}  system={len(s):,}  user={len(u):,}")
PY
```

End to end, **dispatched not local** per `CLAUDE.md`:

```bash
gh workflow run summary.yml && gh run watch --exit-status
aws s3 ls "s3://$S3_BUCKET/summaries/" --recursive
gh workflow run summary.yml   # must find nothing to do, and spend nothing
```

Then the real chain: `gh workflow run report.yml -f day=wednesday`, and
confirm a `summary` run appears on its own within a minute of the report run
completing, with nobody dispatching it. **This is the one step of the
rollout still unexercised** — every `summary` run so far was dispatched by
hand or by a hand-fired EventBridge event, so `workflow_run` has not yet
fired once.

What *is* Observed as of 2026-09-16: the command itself (runs `35051369201`
writing both envelopes, `35051473872` and `35051530130` finding nothing to
do and spending nothing), the append-only push, the run receipts under
`logs/runs/summary/`, `report.sha256` matching the object in the bucket, and
the whole AWS path — a hand-fired `dispatch.summary` event reached
`SummaryRule`, the API destination and GitHub with an empty DLQ.

To exercise the failure path for real rather than in unit tests, point
`GeminiClient`'s injectable `base` at an unreachable host: the run must exit
0 with a warning, write no envelope, and leave the next run free to pick the
summary up.

## Known gaps

- **No summary is ever scored against the report it describes.** Nothing
  checks that the prose is true, that it obeyed the word cap, or that it
  respected the house rules. `prompt_sha256` records which prompt produced
  it; nothing records whether the output was any good.
- **A dropped `workflow_run` event is invisible until the backstop fires.**
  That is a 20-minute window in which nothing knows the summary is late,
  which is the same class of blindness that moved the clock to AWS in the
  first place — reduced in blast radius, not eliminated.
- **`--limit` bounds a backfill.** The cap logs every report it dropped by
  name, so it is not silent, but it is only visible to someone reading the
  run output. A backfilled season takes several runs to catch up.
- **Implicit prompt caching happens but is neither relied on nor
  measured.** The ~22k-token system instruction is identical across all four
  summaries in a week and changes only when the docs do, and one Observed
  call reported `cachedContentTokenCount: 20450` against a 25,529-token
  prompt — while another, minutes later, reported `0`. So the discount is
  real and opportunistic. Nothing requests it, nothing depends on it, and
  the cost figures above assume none of it.
- **No prompt-version pinning beyond `prompt_sha256`.** A prompt change is
  detectable after the fact but not addressable: there is no way to ask for
  "the summary as generated by prompt X", and no automatic re-run of
  existing summaries when the prompt changes. `--force` is the manual
  equivalent.
- **Prior selection orders by filename date, not by week.** For the normal
  weekly cadence these agree. A backfilled re-render of an old week carries
  today's date and would sort as the newest prior, which is wrong.
- **The cost and token figures above are Inferred, not Observed.** They come
  from character counts at ~4 chars/token. The `usage` block in each
  envelope is what will settle them, once runs exist to read.
- **Nothing reads the summaries.** They land in the bucket and stop there —
  no digest, no notification, no rendering beside the report. That is the
  next thing worth building, and deliberately not part of this change.
