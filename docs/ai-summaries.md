# AI summaries

The eight weekly reports are dense — week 2's waiver report is 166 lines with
seven per-slot candidate tables — and none of them says *what changed since
last week* or *what the one decision actually is*. Reading them is the work.
This layer generates a short prose summary of each one and stores it as its
own JSON object under the bucket's `summaries/` prefix.

It also answers a question the reports structurally cannot. Every feed they
are built from — ESPN, Sleeper, nflverse, The Odds API — lags the real world
by hours to days, so nothing in a rendered report knows that a starter was
limited in practice this morning. A second set of calls, **grounded in
Google Search**, takes the full roster and returns the latest news on every
player, stored as a `news` field on the same envelope.

So one envelope, **two layers with opposite epistemic contracts**:

| | Summary | News |
|---|---|---|
| Source | the report, and nothing else | Google Search, and nothing else |
| Outside knowledge | forbidden (`prompt.HOUSE_RULES` #8) | the entire point (`news.NEWS_HOUSE_RULES` #1) |
| Grounding | off | on, and metered |
| Shape | prose, ≤220 words | one structured object per rostered player |
| Calls per report | 1 | up to 3 — starters, bench, IR |

They share no prompt text, no rules and no provenance, and they fail
independently. That opposition is the thing to preserve when changing
either: merging the two prompts would let the summary quietly acquire
outside knowledge, which nothing downstream could detect.

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
| `espn_ff summarize` | `espn_ff/cli.py` | Summarizes and researches every report that still owes either. Takes no report input. Always exits 0. |
| `espn_ff/ai/client.py` | — | `requests`-based Gemini REST client; key in a header, never a URL. Serves both layers off one `generate`. |
| `espn_ff/ai/prompt.py` | — | Pure prompt assembly for the **summary** — no disk, no network, no clock |
| `espn_ff/ai/news.py` | — | Pure prompt assembly for the **news**, plus the response schema and the roster reconciliation |
| `espn_ff/ai/reports.py` | — | Discovery, prior selection, header parsing, the three-state check |
| `espn_ff/ai/summarize.py` | — | Orchestration and the JSON envelope |
| `summary.yml` | `.github/workflows/` | `workflow_run` off `report`, plus dispatch |
| `SummaryRule` + 8 schedules | `infra/scheduler.yaml` | The AWS backstop, 20 min behind each report slot |
| `summaries/` | the bucket | One JSON envelope per summarized report, append-only |

## Two meters, and only one of them is new

The summary layer's cost is settled: roughly **$0.03 per summary, ~$0.24 a
week** at eight summaries *(Observed — two real generations on 2026-09-15:
27,313 and 25,529 prompt tokens, 292 and 253 answer tokens, plus 1,906 and
1,742 **thinking** tokens, against introductory $0.75/1M in and $3.75/1M
out)*.

**Thinking tokens are about a quarter of that cost and all of its risk.**
This model reasons before it writes, those tokens are spent against
`maxOutputTokens` and billed at the output rate, and they outnumber the
answer roughly 6:1. `client.MAX_OUTPUT_TOKENS` is therefore sized for
thinking, not for prose — see "The first deployed run failed" below.

The news layer adds a **second, genuinely metered** meter, and this section
used to say the opposite. Before the news layer existed it read *"Not
credit-metered, so unlike `docs/odds-budget.md`'s ledger there is no guard,
no quota and nothing to reconcile."* That is still true of the summary call
and is **false** of the grounded ones:

| | Allowance | Overage | Unit |
|---|---|---|---|
| Tokens | none — pay as you go | $0.75/1M in, $3.75/1M out | a token |
| Grounded search | 5,000 queries/month, shared across all Gemini 3.x models on the project | $14 per 1,000, i.e. $0.014 each | **a query the model chose to run** |

*(Both rows Documented — Google.)*

The unit is the trap. The vendor bills *"each search query that the model
decides to execute"* — one call that searches nine players is nine billable
uses, not one. So the meter is driven by a multiplier the model picks.

**Observed 2026-09-16**, five real runs against the 15-player week-2 roster
(9 starters, 6 bench, IR empty and skipped):

| | Searches |
|---|---|
| Per report | 7, 10, 11 — call it **~10**, about 0.67 per player |
| Per month, at ~34 reports | **~340** |
| Share of the free 5,000 | **~7%** |

Comfortably inside the allowance, and *below* the one-search-per-player
floor that was assumed before there were numbers. The model reuses results
across players on the same NFL team and skips players it judges quiet,
which is what rule 7 asks for. Worth re-checking when the roster changes
shape — a week with three players on IR adds a third call.

Three responses, and deliberately not a fourth:

1. **It is measured every run.** Every grounded response carries
   `groundingMetadata.webSearchQueries`, so every envelope stores
   `news.search_query_count` and the per-group queries, and every run prints
   the count per report. That is what turned this table from Inferred into
   Observed, and it is what will catch a regression.
2. **The prompt asks for restraint.** `NEWS_HOUSE_RULES` rule 7 asks for one
   search per player and for reusing a result across players on the same NFL
   team. That is a request to a model, not a cap, and it is written down
   here as a request so nobody later mistakes it for a guarantee.
3. **No credit ledger.** `docs/odds-budget.md`'s ledger exists because The
   Odds API's 500-credit period is small enough that one careless run
   exhausts it, and because a spent credit does not come back. 5,000/month
   against an Observed ~340 is a different situation by more than an order
   of magnitude, and `--limit` already bounds a runaway backfill. Building a
   second ledger for this would be over-engineering.

Token spend is the smaller half but not nothing: a grounded call spends far
more **thinking** than a summary does, and unlike the summary it does not
scale with the player count — see `client.NEWS_MAX_OUTPUT_TOKENS` for the
Observed figures and why the budget is sized the way it is.

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

> **`espn_ff summarize` processes every report on S3 that still owes a
> summary, a news block, or both.**

That is strictly better than plumbing the day through. It makes the command
**idempotent**, so the event trigger and the scheduled backstop can both
fire without double-summarizing; and it makes it **self-healing**, so a
summary missed because the model was down gets picked up by the next trigger
instead of being lost. `--day` / `--week` / `--force` remain as filters for
debugging and backfill, and the scheduled path passes none of them.

That check is checked against **both** the pulled cache of everything
already on S3 *and* the output tree this run is writing into. Checking only
the first would make idempotency depend on the S3 push having succeeded —
two runs before a push would summarize the same report twice.

### Three states, not two

Since v2 an envelope can be complete or partial, so "does a file exist" is
no longer enough to decide what a run owes. `reports.envelope_needs` returns
one of three answers:

| What is on disk | What the run does | Calls |
|---|---|---|
| no envelope | generate the summary **and** the news | 1 + up to 3 |
| summary present, `news` is null | **backfill the news only** | up to 3 |
| both present | skip | 0 |
| anything, with `--force` | regenerate both | 1 + up to 3 |

The middle row is what makes a failed grounded call cheap to recover from.
Re-running the summary to reach the news would re-bill ~27k prompt tokens
for an answer already sitting on disk, so `with_news` copies every v1 key —
`summary_markdown`, `model`, `generated_at`, `prompt_sha256`, `usage`,
`prior_reports` and the whole `report` block — across **verbatim** and fills
in only `news`. In particular `generated_at` keeps naming when the *summary*
was written; the news carries its own.

One guard sits on that path. Before backfilling, the run re-hashes the
report on disk and compares it against the envelope's stored
`report.sha256`. If they differ the report was re-rendered after that
summary was written, so **both** are regenerated rather than bolting fresh
news onto a summary of a document that no longer exists. This is the exact
failure `summary.yml`'s `force` input was added for *(Observed 2026-09-16 —
a Wednesday summary written from a pre-refresh render survived the corrected
re-render)*, now fixed for free on the one path that was already reading the
old envelope. It does **not** extend to complete envelopes; see Known gaps.

`--no-news` collapses this back to the original two states. An envelope
written under that flag carries `news: null` with **no** `news_error` —
skipping is not failing — and a later run without the flag backfills the
news without re-billing the summary.

## S3 is the source of truth; the checkout is a backup

Every input this command reads comes from the bucket first:

| Input | Primary (S3) | Backup |
|---|---|---|
| Rendered reports | `reports/` → `.cache/s3-reports` | the git checkout's `reports/`, only when the mirror is empty |
| Existing summaries | `summaries/` → `.cache/s3-summaries` | none — `summaries/` is gitignored, so no local history exists |
| `teams` / `roster-slots` | `latest/out/` → `data/out/` | none — degrades to "unavailable in this run" |
| `weekly-rosters` | `latest/out/` → `data/out/` | none — degrades to a stored `news_error` naming the restore step |

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
| Backstop | Eight EventBridge schedules → `dispatch.summary` → `summary.yml` | Catches a dropped event, or a `report.yml` that never ran |

The backstop is in character for this repo. `docs/aws-scheduling.md` records
why the clock moved to AWS at all: five consecutive missed GitHub-cron slots
on 2026-09-15, one landing 4h30m late, and nothing outside GitHub knowing a
slot was due. A GitHub-internal event is a different reliability profile
from GitHub cron, but it is not an independently observable one either — so
the AWS schedule stays as the thing that notices.

The eight backstops sit 20 minutes behind each report slot: Mon 10:50, Tue
10:20, Tue 11:20, Wed 10:20, Thu 11:20, Fri 11:20, Sat 10:20, Sun 11:50 ET.
Because the job is input-free all eight send an identical, empty payload;
eight exist rather than one so Tuesday's waiver summary lands on Tuesday
rather than waiting for the next slot to come round.

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

## Roster news, and why it is a separate prompt

Three calls per report, not one and not seventeen. One call per **lineup
group** — starters, bench, IR — because that is the unit at which the
question changes:

| Group | What it asks for |
|---|---|
| `starters` | the single most recent item each — injury, practice, usage, matchup |
| `bench` | one line each, and only where something actually changed |
| `ir` | designation and return timeline only |

A group with nobody in it is **skipped, not prompted**: an empty IR is the
normal case for most of a season, and a billed grounded call to be told so
is waste. The skip is recorded (`groups.ir.skipped`) rather than left as an
absence, so a reader can tell "nobody on IR" from "the IR call never ran".

Per-player calls were considered and rejected. They would sharpen recency
slightly and multiply the billed meter by the roster size — and, because
grounding is billed per query rather than per request, the group call does
not actually save searches when the model behaves. It saves them when the
model over-searches, which is the case worth defending against.

### The rules are the inverse of the summary's

`prompt.HOUSE_RULES` #8 says *"do not supply outside knowledge about a
player, a team, or an injury."* `news.NEWS_HOUSE_RULES` #1 says the reverse,
and then spends its length defending the one failure that reversal opens up:

> Every claim must come from a search result you retrieved in this turn. If
> the search returns nothing for a player, set `found` to false and say so.
> **Never** fall back on what you already know about the player from
> training.

That is the load-bearing rule. A model answering from recall produces
confident, fluent, months-stale news that is **indistinguishable from a real
finding** by anything downstream — no hash catches it, no schema catches it,
and the reader has no reason to doubt it. Everything else in the list is
supporting work: date every item or leave the date empty (never default to
today), report a designation in the source's own words rather than
re-expressing it as this repo's `tier`, and confirm the NFL team so a
namesake is rejected rather than reported.

Two more are worth naming:

- **Never turn news into a start/sit call.** The lineup decision belongs to
  the report, and the report cannot see this news. A recommendation here
  would contradict the summary sitting in the same JSON file, and a reader
  would have no way to tell which of the two to believe.
- **The roster row is not news.** Handed a table that includes ESPN's
  `injury_status`, a model will happily hand it back. The table labels that
  column as possibly-days-old context for exactly this reason.

### Coverage is guaranteed in code, not asked for in the prompt

`news.parse_players` reconciles whatever comes back against the roster it
was given, and that reconciliation is the entire reason this field is
structured rather than prose:

- **Every rostered player appears.** One the model skipped is materialised
  with `found: false` and a note. Absence and "no news" are different
  findings, and a prose blob renders them identically.
- **Nobody appears who is not on the roster.** An unrecognised `player_id`
  is dropped and named in the run log.
- **Identity comes from the roster row**, never from the model — name,
  position, team and slot are taken from the DataFrame. The model supplies
  only `player_id` and the news, so a wrong id cannot silently rename one
  player into another.

The response is schema-constrained: `generationConfig.responseFormat`
carries a JSON Schema, and combining that with a built-in tool is a
**Preview** feature of the Gemini 3 series *(Documented — Google)*. It
constrains the shape, not the truth, and a model outside that series loses
the guarantee with no error — so `parse_players` still validates, still
tolerates a stray fenced block, and still raises `NewsFormatError` rather
than storing something partial. That path stays tested.

One vendor detail that costs an afternoon if you trust the documentation:
`responseFormat.text.mimeType` is a protobuf **enum**, not a MIME string.
Google's own example shows `"application/json"`; the API rejects it.

```
Invalid value at 'generation_config.response_format.text.mime_type'
(...v1beta.TextResponseFormat.MimeType), "application/json"
```

The accepted value is `APPLICATION_JSON` *(Observed 2026-09-16)*. It fails
**loudly** with a 400, which is the only reason this was cheap to find — a
silently-ignored schema would have produced plausible prose with no
guarantee behind it and nothing to notice. `news.JSON_MIME_TYPE` holds the
value and a test pins it, because the wrong one is the plausible-looking
one. The older `responseMimeType` + `responseSchema` pair also works on this
endpoint, but `responseFormat` is the surface documented to compose with
tools, so that is the one used.

### The search does not always fire, and that is the real risk

**Observed 2026-09-16: roughly one grounded call in four returns no
`groundingMetadata` at all.** The model answers anyway — fluently,
plausibly, and in the right schema — from recall. That is exactly what rule
1 exists to forbid, and it is invisible from the answer itself.

It is nondeterminism rather than a request problem. The same prompt grounded
3 times out of 3 in one controlled run and 2 of 3 in another, and adding or
removing the response schema changed nothing (3/3 with it, 2/3 without) — so
the Preview combination is not the cause.

Three things handle it, in order:

1. **The prompt asks for the search first.** The misses were not evenly
   distributed: the **bench** call skipped searching far more often than the
   starters call, and the cause was in its own brief. Telling the model that
   "nothing changed" is a legitimate answer — which it is, and which the
   brief still says — also read as permission to reach that answer without
   looking. Adding an explicit *search each of them first*, and naming the
   distinction (`"Nothing changed" is a finding; "I did not look" is not,
   and the two are indistinguishable downstream`), took that call from
   roughly half grounded to **4 of 4** *(Observed 2026-09-16)*. Worth
   remembering when writing the next group brief: any licence to report
   nothing needs pairing with an instruction to look first.
2. **One retry.** `summarize._grounded_call` re-issues a call that came back
   with no grounding, which takes the miss rate from ~1 in 4 to ~1 in 16 for
   the cost of one extra call on the minority of attempts that need it.
   Deliberately not folded into `client.generate`'s retry loop: that layer
   retries *transport* failures, and this is a successful 200 whose content
   is merely unsourced. Merging them would make an ungrounded answer look
   like an HTTP error in the logs.
3. **`news.grounded`, measured and never asserted.** If the retry also comes
   back unsourced the items are still stored — they may well be true, and
   discarding them loses real information — but the envelope says so:
   `groups.<name>.grounded` is false for that call, its `sources` is empty,
   and the top-level `news.grounded` is false if *any* group that ran was
   ungrounded. A skipped group has no opinion and does not drag it down.

That field was hardcoded `true` in the first version of this layer, which
made the single thing a reader would check to tell sourced news from recall
the one thing that could not be wrong. A live run caught it.

## The envelope

One object per summarized report, at
`summaries/<season>/week-NN/<YYYY-MM-DD>-<day_label>-<slug>.json` — the
report tree's own shape, one prefix over.

`schema_version` is **2**. The change from 1 is strictly additive: every v1
key keeps its name, position and meaning, and two new ones join them at top
level. That is not cosmetic — the news-only backfill path copies the v1 keys
off an existing envelope verbatim, which only works because none of them
moved.

```jsonc
{
  "schema_version": 2,
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

  // --- the summary layer, unchanged from v1 ---
  "summary_markdown": "...",
  "model": "gemini-3.8-flash",
  "generated_at": "2026-09-15T21:20:11-04:00",
  "prior_reports": ["reports/2026/week-01/....md"],
  "prompt_sha256": "<64 hex chars of system instruction + user message>",
  "usage": {
    "promptTokenCount": 27313, "candidatesTokenCount": 292,
    "thoughtsTokenCount": 1906, "cachedContentTokenCount": 0,
    "totalTokenCount": 29511
  },

  // --- the news layer, new in v2. null when it failed; see news_error ---
  "news": {
    "model": "gemini-3.8-flash",
    "grounded": true,          // false if ANY group that ran came back unsourced
    "generated_at": "2026-09-16T10:22:04-04:00",
    "roster_week": 2,
    "roster_export": "15-09-2026-weekly-rosters.csv",
    "prompt_sha256": "<64 hex chars of every group prompt concatenated>",
    "search_query_count": 19,
    "players": [
      { "player_id": 4431611, "player_name": "Caleb Williams",
        "position": "QB", "pro_team": "CHI", "lineup_slot": "QB",
        "group": "starters", "espn_injury_status": "ACTIVE",
        "found": true,
        "headline": "Limited in Wednesday practice.",
        "detail": "...", "as_of": "2026-09-16" },
      { "player_id": 4429025, "player_name": "Tre Tucker",
        "position": "WR", "pro_team": "LV", "lineup_slot": "Bench",
        "group": "bench", "espn_injury_status": "ACTIVE",
        "found": false, "headline": null, "detail": null, "as_of": null,
        "note": "the grounded search returned nothing for this player" }
    ],
    "groups": {
      "starters": { "players": 9, "grounded": true, "search_queries": ["..."],
                    "sources": [{ "uri": "...", "title": "..." }],
                    "search_entry_point": "<html>",
                    "usage": { "...": "the five USAGE_FIELDS" } },
      "bench":    { "players": 6, "grounded": true, "search_queries": ["..."], "sources": ["..."],
                    "search_entry_point": "<html>", "usage": { "...": "..." } },
      "ir":       { "skipped": "no players in the ir group for week 2" }
    },
    "usage": { "...": "the five USAGE_FIELDS, summed across groups" }
  },
  "news_error": null
}
```

When the news fails, the two new keys invert and nothing else changes:

```jsonc
"news": null,
"news_error": "bench: HTTP 503 from gemini-3.8-flash:generateContent after 4 attempts"
```

The error lives in a **sibling** key rather than inside `news` on purpose.
It is what makes the completeness test trivially `news is not None`; an
error object stored under `news` would make a failed report look finished to
the next run's three-state check, and the self-healing property would die
silently.

`news.search_query_count` is a **billing** figure, not a quality one. A
report with 40 searches is not better-researched than one with 12; it is
more expensive. `sources` and `search_entry_point` come from the API's own
`groundingMetadata` and never from the model's text — a grounded model cites
redirect URIs it cannot reliably reproduce inline, so a URL it typed into
its answer is not evidence it read anything.

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
five reports still leaves the first two on disk, and writes no envelope for
the third, so the next trigger retries exactly that one.

**And per layer, not per report.** A dead grounded call does not cost the
summary beside it: the envelope is still written, with `news: null` and a
`news_error` naming the cause, and the three-state check picks up the news
next time. The asymmetry is deliberate and runs one way only — a failed
*summary* writes no envelope at all, because there is nothing to attach the
news to. Additivity stacks: the report is additive to the data, the summary
is additive to the report, and the news is additive to the summary.

`summary.yml` inverts this at the edge: because the command is built to exit
0 on everything it anticipates, a **non-zero** exit means something outside
the command broke, and the workflow does fail on it. A report with
`news_error` set is not that — it is the design working, and the workflow
stays green.

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

The news layer adds **no new credential**. It is the same key on the same
endpoint with a `tools` array attached, which is why the grounding budget
above is a quota question rather than a secrets question.

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

# The same for the news prompts, and a preview of how many grounded calls
# this week's roster would actually cost.
python - <<'PY'
from datetime import date
from espn_ff import config
from espn_ff.ai import news
from espn_ff.report.loaders import latest_export
groups = news.roster_groups(latest_export("weekly-rosters"), week=2, team_id=config.TEAM_ID)
for name, df in groups.items():
    if df.empty:
        print(f"{name:<9} empty, would be skipped"); continue
    s = news.system_instruction(name, 2026, 2, date(2026, 9, 16))
    u = news.user_message(df, 2026, 2, date(2026, 9, 16))
    print(f"{name:<9} {len(df):>2} players  system={len(s):,}  user={len(u):,}")
PY
```

*(Observed 2026-09-16 against week 2: `starters` 9 players / 3,809 / 982
chars, `bench` 6 players / 3,769 / 788, `ir` empty and skipped — 15 players
across 2 grounded calls. The news prompts are roughly a seventh the size of
a summary prompt; the cost of this layer is searches, not tokens.)*

**One live call before wiring anything new**, because two spellings in the
request body are the highest-risk unknowns here and a silently-ignored
`responseFormat` produces plausible output with no guarantee behind it:

```bash
python - <<'PY'
from espn_ff.ai.client import GeminiClient, GOOGLE_SEARCH
r = GeminiClient().generate("Answer only from search results.",
                            "Latest injury news on Caleb Williams, Chicago Bears.",
                            tools=GOOGLE_SEARCH)
g = r["grounding"]
print(len(g["search_queries"]), "searches:", g["search_queries"])
print(len(g["sources"]), "sources")
PY
```

A `grounding` of `None` there means the tool never fired and the answer came
from recall.

End to end, **dispatched not local** per `CLAUDE.md`:

```bash
gh workflow run summary.yml && gh run watch --exit-status
aws s3 ls "s3://$S3_BUCKET/summaries/" --recursive
aws s3 cp "s3://$S3_BUCKET/summaries/2026/week-02/<stem>.json" - \
  | jq '.schema_version, .news.search_query_count, .news.groups, (.news.players | length)'
gh workflow run summary.yml   # must find nothing to do, and spend nothing
```

Four checks on that envelope, one per guarantee the shape exists for:

1. `.news.players | length` equals the roster size for that week. Every
   player present, `found: false` where the search came up empty.
2. `.news.groups.*.sources` is non-empty on every group that ran, and no
   `headline` cites a URL the model typed rather than one the API returned.
3. `.summary_markdown` still obeys the 220-word cap and still carries no
   outside knowledge. The two layers must not have bled into each other.
4. `.news.search_query_count` against the projection above. Multiply by ~34
   reports a month and compare to the free 5,000. **A single report issuing
   more than ~50 searches means rule 7 is not landing**, and the prompt needs
   tightening before this runs for a month.

To exercise the backfill for real, hand-edit one envelope in the bucket to
`"news": null`, dispatch, and confirm the run regenerates only the news:
`generated_at` and `prompt_sha256` unchanged, `news.generated_at` fresh, and
the log line reading `backfilled news for …`.

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
summary up. For the news half specifically, fail only the grounded call: the
run must still write the envelope with its summary, `news: null`, a
populated `news_error`, and exit 0.

## Known gaps

- **Nothing enforces the search allowance.** The Observed ~340/month sits at
  ~7% of the free 5,000, but the multiplier is the model's to choose,
  `NEWS_HOUSE_RULES` rule 7 is a request rather than a cap, and there is no
  ledger. The figures come from one roster in one week; a week with a fuller
  IR adds a third call, and a model update could change the search
  appetite entirely. `search_query_count` in every envelope is the thing to
  watch.
- **A grounded call can still run no search at all** and answer from recall.
  The bench brief's search-first wording and the single retry between them
  make this rare, and `news.grounded` records what actually happened — but
  nothing *prevents* it, and nothing re-runs a block that ends up unsourced.
  An ungrounded block is stored, flagged, and never revisited. Whether the
  remaining rate is low enough to ignore is unmeasured over a full week.
- **Tier 1 rate limits are third-party figures, not Google-published ones.**
  ~300 RPM / 1M TPM / 1,000 RPD against a peak of ~16 requests per run is
  ample headroom on every row, but Google's own docs publish no table and
  direct you to the AI Studio dashboard. RPD is the only row a full-season
  `--force` backfill could plausibly approach.
- **No summary is ever scored against the report it describes**, and no news
  item is ever scored against the source that backed it. Nothing checks that
  the prose is true, that it obeyed the word cap, that it respected the
  house rules, or that a `found: true` headline is actually supported by the
  chunk beside it. `prompt_sha256` records which prompt produced it; nothing
  records whether the output was any good.
- **No per-player source attribution.** `sources` is group-level. The API
  returns `groundingSupports` (`startIndex` / `endIndex` /
  `groundingChunkIndices`) which maps response spans back to chunks, so this
  is deferred rather than impossible — the mapping into a structured JSON
  response is just fiddly and was not worth it for the first version.
- **`search_entry_point` is stored but never displayed.** Google's terms
  require showing Search Suggestions wherever grounded results are shown.
  Nothing renders these envelopes yet, so the obligation is recorded here
  and lands on whoever builds the reader.
- **The stale-report guard runs only on the backfill path.** A *complete*
  envelope whose report was later re-rendered is still only fixable with
  `--force`; extending the check to every report would re-hash every report
  on every run and re-bill on any re-render.
- **A permanently failing news call retries on every trigger**, bounded only
  by `--limit`, which counts a cheap news backfill the same as a full
  summary.
- **`NEWS_MAX_OUTPUT_TOKENS` is sized from four Observed calls, not from a
  model.** Thinking does not track player count — the 6-player bench call
  spent 65% more of it than the 9-player starters call — so there is no rule
  to size it by, only headroom over what has been seen. 8000 was the first
  guess and left the worst Observed call at 92% of budget; 16000 is ~2.2x
  that. A `MAX_TOKENS` refusal costs the whole report's news, since a group
  failure is all-or-nothing.
- **Structured-output-with-tools is a Preview feature of the Gemini 3
  series.** It can change or be withdrawn, and a model swap outside that
  series loses the schema guarantee with no error. `NewsFormatError` is the
  fallback and must stay tested. The `responseFormat` field shape is also
  documented wrongly by the vendor (see the `mimeType` enum above), so treat
  their examples as a starting point and verify against a live 400.
- **A dropped `workflow_run` event is invisible until the backstop fires.**
  That is a 20-minute window in which nothing knows the summary is late,
  which is the same class of blindness that moved the clock to AWS in the
  first place — reduced in blast radius, not eliminated.
- **`--limit` bounds a backfill.** The cap logs every report it dropped by
  name, so it is not silent, but it is only visible to someone reading the
  run output. A backfilled season takes several runs to catch up.
- **Implicit prompt caching happens but is neither relied on nor
  measured.** The ~22k-token system instruction is identical across all five
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
- **The summary's token and cost figures are now Observed; the news layer's
  are not.** This bullet used to say all of them were Inferred from
  character counts at ~4 chars/token, which the two real generations on
  2026-09-15 settled for the summary. The news layer starts where the
  summary did: prompt sizes are Observed (see the verification recipe),
  everything downstream of "how many searches does the model choose to run"
  is not.
- **Nothing reads the summaries.** They land in the bucket and stop there —
  no digest, no notification, no rendering beside the report. That is the
  next thing worth building, and deliberately not part of this change. The
  news layer raises the stakes: `search_entry_point` is stored specifically
  for a reader that does not exist yet, and a per-player news block is worth
  more rendered beside its report than sitting in JSON.
