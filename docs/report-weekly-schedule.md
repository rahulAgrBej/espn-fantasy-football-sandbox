# Report weekly schedule

`docs/data-collection-weekly-schedule.md` answers *what should I pull
today, and why does it matter*. This is the consumption-side companion,
and it answers the question that comes immediately after: **given what
has landed on disk by a given hour, what should I read today, and what
decision is due?**

Eight reports across seven days -- Tuesday carries two -- each scheduled
well after the last collection slot it depends on. That margin was
originally an hour because GitHub cron was routinely 5–30 minutes late and
occasionally dropped the slot entirely. It no longer needs to absorb that:
collection is dispatched by EventBridge Scheduler at the exact minute
(`docs/aws-scheduling.md`), so the margin now only has to cover how long a
collection run takes, which is minutes. The 37–82 minutes below are ample,
and the reason the times still look offset rather than aligned is that
they are anchored to the collection slots.

**Monday, Tuesday's week-in-review, Tuesday's waiver wire, and Wednesday
are implemented; the rest of the week is still a specification.**
`espn_ff/report/` is the report engine. `python -m espn_ff report --day
monday` renders the Monday-night call from data already on disk
*(Observed)*, `python -m espn_ff report --day tuesday` renders the
week-in-review, `python -m espn_ff report --day tuesday-waivers` renders
the waiver wire and opening market, and `python -m espn_ff report --day
wednesday` renders the availability watchlist. `espn_ff/cli.py`'s
`REPORTS` dict has no `thursday`, etc. entries yet -- calling `report
--day` with any other value exits with a clear "not implemented" rather
than an empty file. This document is still the specification for the
remaining four reports, not a description of them, which is why no claim
in those sections is tagged **Observed**.

## Overview

Times are ET and stay ET, the same convention as the collection schedule —
these would be EventBridge schedules pinned to `America/New_York`, not GitHub
crons, so the November caveat that used to apply is gone. *(Inferred — each
slot derived from that day's latest collection slot in `infra/scheduler.yaml`,
plus a margin.)*

| Day | Time (ET) | Expression | Report | Waits on | Margin |
|---|---|---|---|---|---|
| Monday | 10:30 | `cron(30 10 ? * MON *)` | Monday night call | Sleeper 08:11, ESPN 09:08, nflverse 09:23 | 67 min |
| Tuesday | 10:00 | `cron(0 10 ? * TUE *)` | Week in review | ESPN 09:08 | 52 min |
| Tuesday | 11:00 | `cron(0 11 ? * TUE *)` | Waiver wire and opening market | ESPN 09:08, Odds `slate` 09:38 | 82 min |
| Wednesday | 10:00 | `cron(0 10 ? * WED *)` | Availability watchlist | Sleeper 08:11, nflverse 09:23 | 37 min |
| Thursday | 11:00 | `cron(0 11 ? * THU *)` | Usage and market | nflverse `--force` 09:53, Odds `props` 10:08 | 52 min |
| Friday | 11:00 | `cron(0 11 ? * FRI *)` | Lineup lock | Sleeper 08:11, Odds `line_movement` 10:08 | 52 min |
| Saturday | 10:00 | `cron(0 10 ? * SAT *)` | Contingency check | Sleeper 08:11, nflverse 09:23 | 37 min |
| Sunday | 11:30 | `cron(30 11 ? * SUN *)` | Pre-lock call | Sleeper 08:11, Odds `pre_lock` 10:38 | 52 min |

Monday's `Waits on` gains nflverse because `NflverseRoutineSchedule` is
`cron(23 9,13,18 * * ? *)` — **daily**, not the Tue/Wed/Sat the collection
doc's table implies *(Observed — `infra/scheduler.yaml`)*. Monday 09:23
therefore delivers the official week-N injury report before the 10:30
report renders. Monday no longer waits on Odds `results`; that job still
runs at 09:38 but feeds nothing Monday reads.

Sunday's slot is the one with a hard deadline behind it: 11:30 ET leaves 90
minutes before 13:00 ET kickoffs, and unlike the old UTC pinning that holds
in November too rather than drifting to 10:30.

When the remaining five are built they belong in `infra/scheduler.yaml`
alongside the collection schedules, with one rule per report workflow,
the same way `ReportTuesdayWaiversSchedule` was added for the 11:00
waiver report. The only odds-dependent margin currently live is that
82-minute Tuesday 11:00 slot; the tightest margins among the rest are the
37-minute Wednesday and Saturday slots, which wait only on feeds that do
auto-retry (`docs/aws-scheduling.md`).

## What every report contains

Four parts, in this order, in all eight.

**The dateline.** Each report leads with an H1 title, a `**Covers**` line
naming the specific date(s) the body actually speaks to, a `**Week N**`
line giving that week's calendar window, and a `**Rendered**` line giving
the generation timestamp — all four in ET. `espn_ff/weeks.py` is the
source for the week window: it reads ESPN's own published season
calendar (`chui_default_platformsettings`'s `scoringPeriods`, the same
cache `EspnClient.current_scoring_period` resolves against) and maps a
week number back to the dates it covers, which nothing else in this repo
did before. *(Observed — `espn_ff/weeks.py`, verified against
`data/raw/2026/chui-default-platformsettings-*.json`'s 18 regular-season
periods.)* The window is the league's own boundary, Tue 03:00 ET → Tue
03:00 ET, pinned to ET **wall-clock** rather than a fixed UTC offset:
period 8 → 9 crosses the Nov 1 DST fallback and both endpoints still land
on Tue 03:00 ET, so the boundary does not drift in November. Two edges in
that calendar are handled explicitly: period 1 is a catch-all offseason
window (Wed 2026-03-25 → Tue 2026-09-15), so week 1's displayed start is
clamped to one week before period 2's start (Tue 2026-09-08) rather than
showing the offseason span; and period 18 ends Mon 2027-01-11, not a
Tuesday, which is reported as ESPN's actual endpoint rather than assumed
to be a Tuesday. When the calendar isn't on disk, the `**Week N**` line
renders `insufficient data` rather than being omitted or guessed.

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

## Monday — Monday night call

The only report of the week issued while a game is still swappable: a
player in that night's game is unlocked at 10:30 ET, so this is the last
chance to bench a hurt starter, start a healthy alternative, or play for
ceiling versus floor based on the live margin.

**What it shows.** The week's remaining game — the `weekday == 'Monday'`
rows from nflverse `schedules` for the current `(season, week)`. The live
margin from `matchups.csv`'s `points_live` for both sides as of the 09:08
refresh, framed as in-progress, never as a result. Which of our starters
and which of the opponent's starters are in that game
(`weekly-rosters.csv`'s `pro_team` against `home_team`/`away_team`), and
the points needed. Then a three-source availability read on every one of
those players.

**What to look out for.** Monday's `matchups.csv` is a *pre-Monday-night*
snapshot: `points_final` reads `0.0` and `winner` reads `UNDECIDED`
correctly, and reporting either as a result is the error this report
exists to prevent. `report_status` is `null` both when the injuries feed
is dark and when a player carries no designation — check whether the
`(season, week)` slice has any rows at all before reading a null as
"healthy". Sleeper's `practice_trajectory` for a Monday-night player is
the *correct* trajectory, not a stale one: that team practised the same
Wed/Thu/Fri we snapshotted, and the Saturday-published final injury report
is the last official word before kickoff.

**Decisions due.** **Binding at 20:15 ET.** Bench or start each of our
Monday-night players, and choose ceiling versus floor from the margin. A
player whose game already kicked off is locked and cannot be moved, so the
entire decision surface is the two Monday-night rosters.

**Swap and drop candidates.** Per at-risk Monday-night starter: bench
players on the same two NFL teams eligible for that slot, then free agents
on those teams, both ranked by `week_projected`. Drop candidates only
where a free-agent add needs the room.

## Tuesday — week in review

**What it shows.** Last week's result, closed out: `matchups.csv`'s
`points_final`, `winner`, and `result`, which only become real numbers
after the 09:08 ESPN `--refresh`; `teams.csv`'s updated record,
`points_for`, and `playoff_seed`; and last week's `weekly-rosters.csv`
split on `started` for a starter-versus-bench points comparison across all
15 roster spots — Monday night included, so this regret table is complete
for the first time.

**What to look out for.** If `winner` still reads `UNDECIDED` and
`points_final` still reads `0.0` on Tuesday, the refresh genuinely
failed — report the week as unclosed rather than as a loss. That is a
different failure mode than seeing the same thing on Monday, where it is
expected (see above): these views are cache-forever, never wired to
`cache.ttl_for`, so reading them off Sunday's cache shows a completed game
as undecided until an explicit `--refresh`. *(Observed — `python -m
espn_ff report --day tuesday --week 2` on Tue 2026-09-15 rendered week 1's
Result section as unclosed for exactly this reason: no `--refresh` had run
since Sunday's cache, so every one of week 1's 6 matchups still read
`UNDECIDED`/`0.0` a full day after that game slate finished. The regret
table, which does not depend on closure, rendered correctly in the same
run: two rows, Chuba Hubbard's 22.2 bench points beating both Rhamondre
Stevenson's 12.0 at RB/WR and Cam Skattebo's 14.1 at RB, for 161.86
optimal points against 151.66 actual.)* Separately, nflverse's
`provisional` flag is still `true` on Tuesday because stat corrections
land Tuesday and Wednesday, so snap share and target share are not
readable yet at any confidence.

**Decisions due.** None are binding. Tuesday's job is to seed the 11:00
waiver report's shortlist and to identify any player whose
`injury_status` makes them IR-eligible — moving one to an IR slot opens a
bench spot without spending a drop.

**Swap and drop candidates.** Rank the roster by optimal-lineup regret:
for each player who started, the highest-scoring bench player eligible for
that slot who outscored them, sorted by the gap. This is a retrospective
measure and explicitly not a start/sit rule — one week of outcome tells
you less than Friday's projection will. The drop list is bench players
with the lowest rest-of-season projection, excluding the only backup at
QB, TE, K, or D/ST, since dropping a sole backup at a one-deep position
trades a real bye-week problem for a marginal add.

## Tuesday — waiver wire and opening market

*(Observed — `python -m espn_ff report --day tuesday-waivers --week 2` on
Tue 2026-09-15, once the odds bugs below were fixed and a real `slate`
had captured. See the known-gaps entry for the bugs themselves.)*

**What it shows.** Waiver settlements from the refreshed
`transactions.csv` — `execution_type`, `is_pending`, and `bid_amount`
per claim — alongside `teams.csv`'s `waiver_rank`, the free-agent pool,
and `implied_team_total` for the coming week from the `slate` job.
*(Observed)* Our waiver rank rendered 4 of 12; seven per-slot add-candidate
blocks (D/ST, K, QB, TE, RB, WR, RB/WR) rendered in that most-constrained-first
order; 5 legal drops were identified; and the WR and RB/WR blocks repeated
the same three candidates verbatim, exactly as the "shares eligible
candidates" note below anticipates.

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
so the move is roster-legal as presented. Each row also carries the
added player's `implied_team_total` as a displayed column *(Observed)* —
the spec above once described "tempering" the add by that number, but
what shipped is a column for the reader to weigh, not a re-rank; a low
team total does not move a candidate down the list on its own.

## Wednesday — availability watchlist

**What it shows.** The first of the three practice days. Sleeper's
`tier`, `practice_participation`, and `practice_trajectory` — which
reads `Wed / — / —` today by construction — plus
`depth_chart_improved` and `depth_chart_promoted` shown alongside
`depth_chart_days_used`, and nflverse's `report_status`. Two behaviors
differ from the spec above as originally written, both deliberate: the
watchlist table is followed by a second table showing ESPN's
`injury_status`, Sleeper's `tier`, and nflverse's `report_status` for the
same players unresolved, side by side, so a disagreement between the
three feeds is visible rather than hidden behind the resolved `tier`
column; and `pos_rank` is omitted entirely rather than shown with a
caveat, since `docs/data-sources.md` flags its source as not week-aligned
and no caveat makes an unusable number usable.

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

`docs/automation.md` and `docs/aws-scheduling.md` own the general
mechanics; four points specific to reports are worth stating here.

**Monday is dispatched.** `infra/scheduler.yaml`'s `report-monday` schedule
fires `.github/workflows/report.yml` at Mon 10:30 ET, 67 minutes after
both Monday's ESPN export and nflverse's routine pull, the two feeds this
report reads. **Tuesday is dispatched twice.** `report-tuesday` fires the
same workflow at Tue 10:00 ET, 52 minutes after Tuesday's ESPN export, the
only feed the week-in-review reads; `report-tuesday-waivers` fires it
again at Tue 11:00 ET, 82 minutes after Odds `slate`'s 09:38 slot — the
tighter of that report's two margins, the other being ESPN's 09:08. All
three share the `ReportScheduleState` flag, like every other family
(Espn, Nflverse, Odds) shares one enable/disable flag across its
schedules. The template's own default for that flag is `DISABLED`, like
every other collection schedule — but the deployed stack's
`ReportScheduleState` was already `ENABLED` before `report-tuesday` was
added, so both `report-tuesday` and `report-tuesday-waivers` activated
immediately on deploy rather than needing a separate rollout step, unlike
a brand-new workflow family. Both Tuesday runs also share
`report.yml`'s `report` concurrency group with `cancel-in-progress:
false`, so a long-running 10:00 job queues the 11:00 dispatch behind it
rather than racing it to `git push` — the 11:00 checkout is guaranteed to
already contain the 10:00 commit. **Wednesday is dispatched.**
`infra/scheduler.yaml`'s `report-wednesday` fires `report.yml` at Wed
10:00 ET, 37 minutes after nflverse's routine pull — the tightest report
margin of the week. It shares the `ReportScheduleState` flag and
`report.yml`'s `report` concurrency group with the other three.

**Reports read state and own none of it, except the reports themselves.**
`report.yml` restores every state subtree it needs and pushes none of
`data/` back — an empty `push-paths`, which `scripts/s3_sync.sh`'s
`push-state` now correctly treats as "own nothing" rather than its old,
dangerous default of "own everything." `data/out/` exports the reports
read (`matchups`, `weekly-rosters`, `player-pool`, `roster-slots`,
`teams`, `transactions`) are never restored by the ordinary state restore
either — they come from S3's `latest/out/` convenience copy via a new
`restore-out` subcommand, since `data/out/` is otherwise purely an
archive destination. The waiver report additionally reads `data/odds/`,
restored via `state-paths`' `odds` subtree — the first report to read a
subtree owned by a metered workflow (`odds.yml`), though still read-only:
`report.yml`'s `push-paths` stays empty, so `odds.yml` remains that
subtree's sole writer.

**Committing to the repository is resolved: reports live in both places,
kept as mirrors.** `reports/<season>/week-<NN>/` is git-tracked in this
repo, and `report.yml` commits its own output there (`contents: write` —
the one workflow here with it) before mirroring the identical tree to a
new `s3://$BUCKET/reports/` prefix (a `sync-reports` subcommand, synced
`--delete`, safe specifically because `actions/checkout` restores the
full git history first, so the local copy is always complete). Reading a
report therefore never requires AWS access — it is a normal file in the
repo — while S3 keeps a durable copy of the same tree independent of git.

**Path convention:**
`reports/<season>/week-<NN>/<YYYY-MM-DD>-<day>-<slug>.md`. ISO dates,
not the `dd-mm-yyyy` form `data/out/` uses, for exactly the reason
`docs/automation.md` gives for the archive prefix — ISO sorts lexically
and `dd-mm-yyyy` does not. One consequence worth pinning down:
`scripts/s3_sync.sh`'s `cmd_archive` parses `dd-mm-yyyy-<dataset>.csv`
by byte offset, so reports must be archived through a separate path and
must never be written into `data/out/`, where they would be mis-keyed.

`<day>` in that path is a **filename segment, distinct from the `--day`
key** passed on the command line. `espn_ff/cli.py`'s `REPORTS` maps each
key to a `(build_fn, day_label, slug)` 3-tuple, and `<day>` above is
`day_label`, not the key: `--day tuesday-waivers` writes
`<YYYY-MM-DD>-tuesday-waiver-wire.md`, sharing Tuesday's `tuesday`
filename segment with `--day tuesday`'s `<YYYY-MM-DD>-tuesday-week-in-review.md`
while remaining a distinct `REPORTS` entry with its own `slug`. This is
new behavior from `REPORTS` becoming 3-tuples rather than a bare
key-to-function map, and was previously undocumented here.

## Known gaps

- **Filenames still carry the *render* date, not the covered date.** The
  dateline above puts the covered week and date in the body, but
  `reports/<season>/week-<NN>/<YYYY-MM-DD>-<day>-<slug>.md`'s date and
  `<NN>` are still keyed on when the report ran, not what it reviews —
  e.g. `week-02/2026-09-15-tuesday-week-in-review.md` is a week-1 report
  filed under week 2, visible in the body's `**Covers**`/`**Week 1**`
  lines now, still not in the path.
- **Monday, Tuesday's week-in-review, and Tuesday's waiver wire are the
  only reports that have been Observed running.** Wednesday is built but
  not yet Observed — `report --day wednesday` exists and its tests pass,
  but no scheduled or dispatched run has produced a real artifact from it.
  The other four report slots are still intent, not description — every
  time, threshold, and behavior in those sections is a specification
  until something has run a real NFL week.
- **`practice_trajectory` reads `— / — / —` for every row on Wednesday's
  first run.** This is the standing gap noted throughout
  `docs/data-sources.md`: the daily slim-snapshot store has not yet
  accumulated three days of history, so the trajectory column Wednesday's
  watchlist is built around is empty by construction on day one, not
  because anything failed.
- **`freshness()` never validates a `data/out` export's own content**, only
  each feed's last-run sidecar (`last_run.json` / `manifest.json` /
  `.meta.json`). If the morning's ESPN export silently fails to produce a
  fresh `matchups.csv`, `latest/out/matchups.csv` is whatever a prior
  successful run wrote, and Monday's report renders off it with no stale
  flag anywhere in the output — a pre-existing gap this pipeline had before
  Monday's report started actually running, made consequential now that it
  does.
- **Monday renders at 10:30 ET for a 20:15 ET kickoff**, roughly ten hours
  early, and official inactives drop about 90 minutes before kickoff in no
  feed this pipeline touches. A second, later Monday slot was considered
  and deferred.
- **Whether an unowned player can actually be added on a Monday is a
  league waiver setting recorded in no artifact here.** The free-agent
  half of Monday's alternatives may be unactionable, and the report must
  state that as an assumption rather than imply it.
- **Which slots ESPN leaves unlocked on a Monday** — specifically
  bye-week and already-played players — is platform behavior observed
  nowhere in this repo. Only players on the two Monday-night teams are
  known-unlocked.
- **2026 has exactly one Monday game in every regular-season week**, but
  that is a property of this season's schedule, not a rule. The report
  handles zero Monday games (week 18) and more than one, but neither case
  has been Observed against a real slate.
- **The odds layer produced data for the first time on 2026-09-15, and it
  exposed two real bugs, both now fixed.** `TOTALS_DEDUPE_KEYS` (and
  `PROPS_DEDUPE_KEYS` the same way) omitted `outcome_name`, so a two-sided
  market's Over/Under or Yes/No rows collided on write within a single
  capture and only the last-written side survived — the totals market
  lost its Over side entirely, on every run, until fixed. Separately,
  `consensus_line()` applied an Over/Yes outcome filter to the `spreads`
  market, whose rows are already split one-per-team with the team's own
  name as the outcome label, never "Over" — that filter matched nothing
  and produced `NaN` for every team, on every run, regardless of the
  dedupe bug. Together these meant `implied_team_total` rendered
  "insufficient data" for all 32 teams even on a fresh, non-stale
  `slate` capture — *(Observed)* a real 6-credit `slate` run on
  2026-09-15 reproduced exactly this before the fix, and real numbers
  after it (`espn_ff/odds/store.py`, `espn_ff/odds/projections.py`).
  Thursday's props, Friday's line movement, and Sunday's premise still
  await their own first live run to confirm the fix covers their shapes
  too — the fix is general (both dedupe keys, not just totals') but only
  the totals path has been Observed end-to-end. There is still no
  historical endpoint, so no window before a report's first real run can
  ever be backfilled.
- **`practice_trajectory` currently reads `— / — / —` for every row.**
  Only two daily Sleeper snapshots exist and neither falls in a
  Wednesday–Friday window, so the single most decision-relevant Sleeper
  signal is presently empty. Friday's report is the one this most
  degrades.
- **The free-agent pool is a derivation, not an artifact.** Nothing on
  disk distinguishes free agents from rostered players. The anti-join
  itself lives in `espn_ff/report/pool.py`, built for Monday's
  alternatives section and now also the basis for Tuesday's waiver
  report's add candidates.
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
  line**, a guard aimed at a leaked API key. *(Observed — 2026-09-15
  `slate` response)* Odds API `event_id` values are indeed 32-hex
  strings (32 lowercase hex characters), confirming the prior
  inference. `team_totals()` already drops `event_id` before it reaches
  the renderer, and `grep -cE '\b[0-9a-f]{32}\b'` against the committed
  Tuesday waiver report returns `0` — the guard is a non-issue for this
  report as written, but would fire immediately if any future section
  ever surfaced a raw event id.
- **Tuesday's regret table falls back to a pure-position slot-eligibility
  map for any player dropped since the reviewed week** (no
  `player-pool.csv` row that week), and its rest-of-season projection is
  derived as `season_projected - season_points` rather than published by
  any feed *(Inferred)*. Both are named in the report's footer whenever
  they fire, but neither is a vendor-asserted fact.
