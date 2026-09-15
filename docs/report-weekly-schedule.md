# Report weekly schedule

`docs/data-collection-weekly-schedule.md` answers *what should I pull
today, and why does it matter*. This is the consumption-side companion,
and it answers the question that comes immediately after: **given what
has landed on disk by a given hour, what should I read today, and what
decision is due?**

Seven reports, one per day, each scheduled well after the last collection
slot it depends on. That margin was originally an hour because GitHub cron
was routinely 5–30 minutes late and occasionally dropped the slot entirely.
It no longer needs to absorb that: collection is dispatched by EventBridge
Scheduler at the exact minute (`docs/aws-scheduling.md`), so the margin now
only has to cover how long a collection run takes, which is minutes. The
36–52 minutes below are ample, and the reason the times still look offset
rather than aligned is that they are anchored to the collection slots.

Nothing here has run yet. **No report engine exists in this repository**
— `espn_ff/cli.py`'s `COMMANDS` dict holds eleven commands, all of them
collection or derivation, and nothing in the codebase writes a markdown
file. This document is the specification for that build, not a
description of it, which is why no claim below is tagged **Observed**.

## Overview

Times are ET and stay ET, the same convention as the collection schedule —
these would be EventBridge schedules pinned to `America/New_York`, not GitHub
crons, so the November caveat that used to apply is gone. *(Inferred — each
slot derived from that day's latest collection slot in `infra/scheduler.yaml`,
plus a margin.)*

| Day | Time (ET) | Expression | Report | Waits on | Margin |
|---|---|---|---|---|---|
| Monday | 10:30 | `cron(30 10 ? * MON *)` | Week in review | ESPN 09:08, Odds `results` 09:38 | 52 min |
| Tuesday | 10:30 | `cron(30 10 ? * TUE *)` | Waiver wire and opening market | ESPN 09:08, Odds `slate` 09:38 | 52 min |
| Wednesday | 10:00 | `cron(0 10 ? * WED *)` | Availability watchlist | Sleeper 08:11, nflverse 09:23 | 37 min |
| Thursday | 11:00 | `cron(0 11 ? * THU *)` | Usage and market | nflverse `--force` 09:53, Odds `props` 10:08 | 52 min |
| Friday | 11:00 | `cron(0 11 ? * FRI *)` | Lineup lock | Sleeper 08:11, Odds `line_movement` 10:08 | 52 min |
| Saturday | 10:00 | `cron(0 10 ? * SAT *)` | Contingency check | Sleeper 08:11, nflverse 09:23 | 37 min |
| Sunday | 11:30 | `cron(30 11 ? * SUN *)` | Pre-lock call | Sleeper 08:11, Odds `pre_lock` 10:38 | 52 min |

Sunday's slot is the one with a hard deadline behind it: 11:30 ET leaves 90
minutes before 13:00 ET kickoffs, and unlike the old UTC pinning that holds
in November too rather than drifting to 10:30.

When these are built they belong in `infra/scheduler.yaml` alongside the
collection schedules, with one rule per report workflow. The 52-minute Monday
and Tuesday margins are the tightest real constraint, since both wait on an
odds job that does not auto-retry — a failed odds slot that needs a person
will not have been re-fired by 10:30.

## What every report contains

Three parts, in this order, in all seven.

**The freshness header.** Each report opens by stating, per feed, the
artifact's own timestamp and staleness flag: `fetched_at` from
`data/sleeper/last_run.json`, `last_updated` from
`data/nflverse/manifest.json`, `captured_at` from the odds parquet plus
that job's entry in `data/odds/last_run.json`, and `fetched_at` from the
ESPN cache's `.meta.json` sidecar. **Never a file's mtime.**
`docs/odds-budget.md` is explicit that a budget-aborted job leaves the
prior snapshot in place with no visible difference on disk, so the mtime
of a parquet file says when a job ran, not when its contents were true.

**The body.** Decisions due today first, then the tables that support
them. A report that buries the decision under the data has the ordering
backwards.

**What this report cannot see.** A closing footer naming that day's
blind spots — any feed that came back stale, plus the standing gaps in
the "Known gaps" section below. A report that silently omits a section
because its input was missing is indistinguishable from one that found
nothing worth saying.

The rule that ties those together: **when an input is stale or missing,
the section that depends on it renders "insufficient data", not a
number.** `docs/data-sources.md` already sets this precedent for the
deferred category-level diagnostic — render the words rather than a
figure computed on history that isn't there.

## Monday — week in review

**What it shows.** Last week's result, closed out: `matchups.csv`'s
`points_final`, `winner`, and `result`, which only become real numbers
after the 09:00 ESPN `--refresh`; `teams.csv`'s updated record,
`points_for`, and `playoff_seed`; and last week's `weekly-rosters.csv`
split on `started` for a starter-versus-bench points comparison across
all 15 roster spots.

**What to look out for.** If `winner` still reads `UNDECIDED` and
`points_final` still reads `0.0`, the ESPN refresh did not land — report
the week as unclosed rather than as a loss. This is not hypothetical:
these views are cache-forever, never wired to `cache.ttl_for`, so
reading them off Sunday's cache shows a completed game as undecided.
Separately, nflverse's `provisional` flag is still `true` on Monday
because stat corrections land Tuesday and Wednesday, so snap share and
target share are not readable yet at any confidence.

**Decisions due.** None are binding. Monday's job is to seed Tuesday's
waiver shortlist and to identify any player whose `injury_status` makes
them IR-eligible — moving one to an IR slot opens a bench spot without
spending a drop.

**Swap and drop candidates.** Rank the roster by optimal-lineup regret:
for each player who started, the highest-scoring bench player eligible
for that slot who outscored them, sorted by the gap. This is a
retrospective measure and explicitly not a start/sit rule — one week of
outcome tells you less than Friday's projection will. The drop list is
bench players with the lowest rest-of-season projection, excluding the
only backup at QB, TE, K, or D/ST, since dropping a sole backup at a
one-deep position trades a real bye-week problem for a marginal add.

## Tuesday — waiver wire and opening market

**What it shows.** Waiver settlements from the refreshed
`transactions.csv` — `execution_type`, `is_pending`, and `bid_amount`
per claim — alongside `teams.csv`'s `waiver_rank`, the free-agent pool,
and `implied_team_total` for the coming week from the `slate` job.

**What to look out for.** **`player-pool.csv` carries `on_team_id = 0`
on every row.** It is pulled from the ownership-free
`leaguedefaults/3` pool (`config.PLAYER_POOL_ID`), so that column
cannot distinguish a free agent from a rostered player — the pool only
exists as an anti-join against all twelve teams' `weekly-rosters.csv`
for the current week. A report that reads `on_team_id` directly will
present the entire league's rosters as available. Two smaller traps:
`is_pending = True` means waivers have not settled, so a claim shown as
won may not be; and `trending_add` is display-only context,
`espn_ff/sleeper/signals.py:8` being explicit that it must never feed a
score or a sort.

**Decisions due.** Submit or adjust waiver claims, and decide which
roster spot each add displaces. `percent_owned` in this file has no
final state — it moves continuously vendor-side *(Inferred, per
`docs/data-sources.md`)* — so it is a rough ownership signal, not a
settled one.

**Swap and drop candidates.** Ranked add candidates from the derived
free-agent pool, scored by `week_projected` against the weakest bench
player eligible for the same slot, with each add paired to a named drop
so the move is roster-legal as presented. Temper each by the added
player's `implied_team_total`: a low team total is reason to discount a
bench-and-hope add before any practice or injury signal exists at all.

## Wednesday — availability watchlist

**What it shows.** The first of the three practice days. Sleeper's
`tier`, `practice_participation`, and `practice_trajectory` — which
reads `Wed / — / —` today by construction — plus
`depth_chart_improved` and `depth_chart_promoted` shown alongside
`depth_chart_days_used`, and nflverse's `report_status`.

**What to look out for.** A single Wednesday `DNP` is one-third of a
trajectory, not a call, and `tier` is derived from the current snapshot
alone so it will move as Thursday and Friday land. Read
`depth_chart_days_used` before trusting either depth-chart delta: it
records the gap actually used, which falls back to the oldest available
snapshot when none exists at exactly `--depth-lookback` days. And
`pos_rank` comes from `depth_charts`, which `docs/data-sources.md`
flags as **not week-aligned** — it is an append-only log with no week
column, so reading it as this week's depth is wrong. `report_status` is
`null` both when the injuries feed is unavailable and when a player has
no designation; those two cases are indistinguishable in that column.

**Decisions due.** None binding, unless our league's waiver deadline is
Wednesday night — which is not recorded in any artifact this pipeline
collects, so the report must state the assumption rather than imply it.
Today's output is a contingency list, not an action.

**Swap and drop candidates.** Watchlist only. Starters whose `tier` is
`OUT`, `HIGH_RISK`, or `COIN_FLIP`, ranked by projected points at risk,
each shown with the best bench replacement eligible for that slot. No
drop candidates today: dropping on one day of practice data discards a
player before the signal that would justify it exists.

## Thursday — usage and market

**What it shows.** The canonical read of last week, after the 09:00
`nflverse --force` run: `offense_pct`, `snap_pct_delta_1w`,
`snap_pct_trend`, `target_share`, `air_yards_share`, `wopr`, and
`targets_per_snap`. Plus the `props` job's per-player `fantasy_points`,
converted through the league's own scoring rather than a hardcoded
points-per-stat table, and `practice_trajectory` at `Wed / Thu / —`.

**What to look out for.** If `provisional` is still `true`, the
Thursday `--force` run did not land and these are not canonical numbers
— say so rather than presenting them as final. `snap_pct_delta_3w` is
`NaN` before week 4 and `snap_pct_delta_1w` before week 2; render the
gap, not a zero. On the props side, rows with `match_source =
unmatched` are kept and unscored by design, so report how many rather
than dropping them silently — the odds feed carries no player id and
often no team, making it the one name join in this repo with nothing to
fall back on. An empty props response means the market has not
consolidated for that game, not that no props exist.

**Decisions due.** **The Thursday-night start/sit is binding at
kickoff** — the only hard call today. Make it knowing the practice
trajectory behind it is two days deep rather than three; a short week
gives genuinely less signal than Sunday's players will have by Friday.

**Swap and drop candidates.** Starters whose one-week snap share fell
against a bench player trending the other way, ranked by the projection
gap and shown with `wopr` so a target-share story is visible next to a
snap-count one. Separately, any player whose prop-derived
`fantasy_points` diverges materially from ESPN's own `projected` — as a
tiebreaker to investigate, never as an override, since the two numbers
are built from different inputs and disagreement is expected.

## Friday — lineup lock

The week's most consequential report.

**What it shows.** The complete `practice_trajectory` (`Wed / Thu /
Fri`) and `tier` at its most informed point of the week; the
`line_movement` job's spread and total deltas against Tuesday's
`slate`, compared by `captured_at` rather than by run order; and a full
optimal-lineup solve across the nine starting slots — QB, RB, RB,
RB/WR, WR, WR, TE, D/ST, K — respecting each player's `eligible_slots`.

**What to look out for.** A trajectory reading `— / — / —` means a
Sleeper day was missed, and that is permanent rather than deferred:
`practice_trajectory` is reconstructed from our own consecutive daily
snapshots and Sleeper publishes no history to backfill from. Friday is
the report most degraded by a missed collection day, and it should say
so explicitly instead of quietly falling back to `injury_status` alone.
A sharp line move since Tuesday is the market's own read on the same
injury news the rest of this report is built from — worth cross-checking
against, not deferring to.

**Decisions due.** **Lock the Sunday lineup**, except for slots
deliberately held open for Sunday's `pre_lock` read. Holding a slot open
should be a stated choice in the report, so Sunday knows what it owes an
answer on.

**Swap and drop candidates.** A per-slot table of current starter versus
recommended starter, each row naming the rule that fired: a projection
gap at or above the configured threshold, a `tier` downgrade to `OUT` or
`HIGH_RISK`, or a team total that moved down since Tuesday's open. The
full six-player bench is listed ranked by projection regardless, so the
flex decision is visible rather than asserted. Drop candidates are
limited to players who would make room for a weekend streaming add at
D/ST or K.

## Saturday — contingency check

**What it shows.** A diff against Friday, and deliberately little else.
New `OUT` or `DOUBTFUL` designations since Friday's snapshot,
practice-squad elevations, and depth-chart moves. No odds job runs
Saturday and the report should not imply one did — `docs/odds-budget.md`
has no Saturday slot because there is no market event between Friday's
`line_movement` and Sunday's `pre_lock`.

**What to look out for.** Saturday is quiet by design, not by accident,
and nothing surfaced today should override Friday's read on its own. A
change here is almost always a designation change rather than new
information about usage or role.

**Decisions due.** Hold or adjust a single slot. Confirm no starter is
on a bye and none is sitting in an IR slot where they cannot score.

**Swap and drop candidates.** Only players whose `tier` changed since
Friday, each paired with the replacement Friday already computed, so
the swap is one step rather than a fresh evaluation. If no tier moved,
the section says so in a line.

## Sunday — pre-lock call

**What it shows.** The `pre_lock` job's featured lines and its props for
still-undecided slots, the final `tier` and `injury_status` read from
the 08:00 Sleeper run, and the specific slots Friday left open.

**What to look out for.** Check `data/odds/last_run.json`'s `stale`
flag for `pre_lock` specifically — the file is keyed per job so one
job's staleness never masks another's, and a budget-aborted `pre_lock`
leaves Friday's lines in place looking identical to a fresh pull. This
is the one job permitted to draw down the reserve, which
`docs/odds-budget.md` owns the rules for; the report's only job is to
verify it actually ran.

**Decisions due.** **The final lock, before 13:00 ET.** After kickoff
this report is history, and it should carry its own generation timestamp
prominently enough that a stale tab is obvious.

**Swap and drop candidates.** Only the slots Friday held open, ranked by
the `pre_lock` consensus, plus any starter whose `tier` moved to `OUT`
overnight shown with the bench replacement already identified. The
footer must state plainly that official inactives drop roughly 90
minutes before kickoff and appear in no feed this pipeline touches — a
report generated at 11:30 cannot see them, and saying so is the
difference between a limitation and a silent error.

## How these will run

The mechanics belong to `docs/automation.md`; three points specific to
reports are worth stating here.

**Reports read state and own none of it.** A report workflow restores
every state subtree and pushes none back — an empty `push-paths` — which
preserves the one-writer-per-subtree rule that makes the `--delete`
mirror of `state/` safe. Rendered reports are archived, never mirrored
into `state/`.

**Committing to the repository is a new capability.** Every existing
workflow runs with `contents: read` and persists solely to S3; nothing
in this repo has ever done a `git commit` from CI. Writing to `reports/`
requires `contents: write` on a public repository, and that is a
deliberate trade rather than an implementation detail — it should be
decided on the record, not discovered in a diff.

**Path convention:**
`reports/<season>/week-<NN>/<YYYY-MM-DD>-<day>-<slug>.md`. ISO dates,
not the `dd-mm-yyyy` form `data/out/` uses, for exactly the reason
`docs/automation.md` gives for the archive prefix — ISO sorts lexically
and `dd-mm-yyyy` does not. One consequence worth pinning down:
`scripts/s3_sync.sh`'s `cmd_archive` parses `dd-mm-yyyy-<dataset>.csv`
by byte offset, so reports must be archived through a separate path and
must never be written into `data/out/`, where they would be mis-keyed.

## Known gaps

- **Nothing in this document has been Observed.** No report engine
  exists; every time, threshold, and behavior here is intent. Treat the
  schedule as a specification until something has run a real NFL week.
- **The odds layer has never produced data.** `data/odds/ledger.db`
  holds zero rows in all three of its ledger tables, and no parquet,
  `last_run.json`, or `league_scoring.json` exists yet. Every
  odds-dependent section — Tuesday's team totals, Thursday's props,
  Friday's line movement, Sunday's entire premise — renders
  "insufficient data" until the first `slate` job succeeds. There is no
  historical endpoint, so the window before that first run cannot be
  backfilled, ever.
- **`practice_trajectory` currently reads `— / — / —` for every row.**
  Only two daily Sleeper snapshots exist and neither falls in a
  Wednesday–Friday window, so the single most decision-relevant Sleeper
  signal is presently empty. Friday's report is the one this most
  degrades.
- **The free-agent pool is a derivation, not an artifact.** Nothing on
  disk distinguishes free agents from rostered players; every waiver
  section here depends on an anti-join that does not exist in the
  codebase yet.
- **Our league's waiver-processing night is recorded in no artifact.**
  Tuesday's and Wednesday's claim-deadline guidance is therefore
  league-setting dependent and unverified — it should be stated as an
  assumption in the report, not presented as a schedule.
- **Gameday inactives are in no feed.** The last roughly 90 minutes
  before kickoff are invisible to this pipeline.
- **No report is ever scored against what happened.** Nothing here
  learns whether its own calls were right; `docs/data-sources.md`
  already defers the `recommendations` backtest loop, and until that
  exists the thresholds in Friday's lineup solve are chosen rather than
  fitted.
- **`.githooks/pre-commit` blocks any bare 32-hex string in an added
  line**, a guard aimed at a leaked API key. The Odds API's `event_id`
  values are plausibly that shape *(Inferred — never verified against a
  real response, since no odds data has been captured)*, which would
  make a report that prints raw event ids uncommittable locally. Worth
  checking against the first real `slate` response before the reports
  render any event id.
