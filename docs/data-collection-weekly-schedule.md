# Data collection weekly schedule

This is the operational companion to `docs/data-sources.md` (field-level
freshness per source) and `docs/odds-budget.md` (the Odds API's metered
job schedule). Those two documents answer *how stale can a field be* and
*what does a pull cost*; this one answers the question a person actually
running this pipeline has day to day: **what should I pull today, and why
does it matter for this week's lineup and waiver decisions?**

**This schedule runs in GitHub Actions, on a clock AWS owns** — see
`docs/automation.md` for the workflow set, the S3 state/archive split and the
runbook, and `docs/aws-scheduling.md` for the EventBridge schedules that fire
them. Each day below still reads as guidance rather than as a job definition:
the point of this document is *why* a given day's data matters for a lineup or
waiver decision, which is what you need whether the pull was automated or you
ran it by hand. Every workflow also accepts `workflow_dispatch`, so any command
here can still be fired deliberately.

## Overview

Times are ET and stay ET. The schedules are pinned to the `America/New_York`
zone rather than to a UTC offset, so every slot below holds its wall-clock time
when DST ends in November. *(Observed — EventBridge expressions in
`infra/scheduler.yaml`, evaluated across the 2026-11-01 boundary.)*

| Day | Time (ET) | Data metric | Data source |
|---|---|---|---|
| Monday | 08:11 daily | Daily player status refresh | Sleeper — `sleeper` |
| Monday | 09:08 | Scores through Sunday night — a pre-Monday-night snapshot — and weekend transactions | ESPN — `--refresh` on `matchups.csv` / `transactions.csv` |
| Monday | 09:23 / 13:23 / 18:23 | Official week-N injury report, including the Monday night game | nflverse — routine 3x/day pull |
| Monday | 09:38 | Prior week's game results (`daysFrom=3`) | The Odds API — `results` job |
| Tuesday | 09:08 | Settled matchup results after Monday night, plus this week's waiver claims (still pending ahead of tonight's run) | ESPN — `--refresh` on `matchups.csv` / `transactions.csv` |
| Tuesday | 09:23 / 13:23 / 18:23 | Stat corrections begin landing | nflverse — `stats_player` feed, routine 3x/day pull |
| Tuesday | 09:38 | Opening spreads/totals for the coming week | The Odds API — `slate` job |
| Wednesday | 08:11 daily | Practice participation, day 1 of 3 | Sleeper — `sleeper` |
| Wednesday | 09:08 | Waiver settlements from Tuesday night's processing run | ESPN — `--refresh` on `transactions.csv` |
| Wednesday | 09:23 / 13:23 / 18:23 | Stat corrections continue landing | nflverse — routine 3x/day pull |
| Wednesday | No scheduled job | Player-props market opens (nothing decision-relevant yet) | The Odds API — market open |
| Thursday | 08:11 daily | Practice participation, day 2 of 3 | Sleeper — `sleeper` |
| Thursday | 09:53 | First canonical read of the prior week's stats (`provisional` resolves) | nflverse — `--force` refresh |
| Thursday | 10:08 | Player-props snapshot | The Odds API — `props` job |
| Friday | 08:11 daily | Practice participation, day 3 of 3 — `practice_trajectory` complete | Sleeper — `sleeper` |
| Friday | 10:08 | Line movement since Tuesday's open | The Odds API — `line_movement` job |
| Saturday | 09:23 / 13:23 / 18:23 | Routine background refresh only, no new decision-relevant data | nflverse — routine 3x/day pull |
| Sunday | 10:38 | Featured + undecided-slot prop lines, final line before lock | The Odds API — `pre_lock` job (critical priority) |
| Sunday | 13:08–00:38 Mon | Live scoring, live rosters | ESPN — `LIVE_TTL`-gated `--refresh` (weekly-rosters, matchups), every 30 min |

## Monday

**What gets pulled and why.** Sunday's games are over, but the week is
*not* settled yet — every 2026 regular-season week has exactly one Monday
night game *(Observed — `data/raw/nflverse/schedules/games.parquet`, 2026
REG, `weekday == 'Monday'` returns one row for each of weeks 1–17)*, so
Monday's ESPN pull is a pre-Monday-night snapshot, not a closed week. Pull
ESPN's `matchups.csv` and `transactions.csv` with `--refresh` — both
views are cached forever once fetched (`data-sources.md` notes neither is
wired to `ttl_for`), so a live-week `points_live`/`winner` value from
Sunday will still read `UNDECIDED`/stale until you explicitly refresh. Run
`sleeper` for the daily status snapshot; nflverse's routine 3x/day pull
also lands the official week-N injury report today, covering the Monday
night game. Run the Odds API `results` job (`daysFrom=3`, cost 2 credits)
to record final scores — it cannot include tonight's game (see Known
caveats).

**Implications of the data.** `points_final` and `winner` only become
trustworthy once you refresh, and even then Monday's refresh only closes
out Sunday's games — reading Monday's matchup data off Sunday's cache
would silently show `0.0`/`UNDECIDED` for games that already ended, and
the two Monday-night teams stay `UNDECIDED`/`points_live` all day by
design. nflverse's own stat corrections have *not* landed yet at this
point in the week (they land Tuesday–Wednesday), so Monday's
`stats_player`/snap-share numbers for Sunday's games are still
provisional.

**Fantasy strategy implications.** Monday's pull exists to support a
start/sit call on tonight's game, not to close the book on the week — the
week settles Tuesday, after Monday night has been played and refreshed.
This league processes waivers Tuesday night into Wednesday *(Documented —
league setting, per the league manager)*, so nothing has resolved yet
today: Monday is the day to research the free-agent pool and submit or
adjust claims ahead of tonight's Tuesday-night deadline, not to check
what already settled. Check the official injury report for both
Monday-night rosters while you're at it. Don't over-read Monday's raw
box-score stats for target share or snap share yet; the nflverse
`provisional` flag on Sunday's games will still be `true`, and the
numbers can move before Thursday's canonical read.

## Tuesday

**What gets pulled and why.** The Odds API's `slate` job opens Tuesday
morning specifically because spreads/totals for the coming week open
"Sunday night for the coming week" per `odds-budget.md`'s market-open
table — Tuesday is the first convenient, deliberate pull after that
window opens. Another ESPN `--refresh` on `matchups.csv` now settles the
week for good, Monday night included. `transactions.csv` is refreshed at
the same 09:08 slot, but this league processes waivers Tuesday night into
Wednesday *(Documented — league setting, per the league manager)*, and
09:08 runs roughly twelve hours *before* that processing run — so this
week's claims still read `is_pending = True` at this pull, and the
"Waiver settlements" a Tuesday-morning read shows are last week's, not
this week's. nflverse's `stats_player` corrections begin landing
Tuesday–Wednesday as the league processes officiating/scoring corrections
from the weekend.

**Implications of the data.** Tuesday is the first day matchup outcomes
and season-long stat lines can actually be treated as final — this is the
point the week actually closes, not Monday. The week itself runs
Tue 03:00 ET → Tue 03:00 ET, the league's own boundary per ESPN's
published calendar; see `docs/report-weekly-schedule.md`'s "What every
report contains" for how `espn_ff/weeks.py` derives that window (and its
two calendar edges) rather than restating it here. Tuesday's opening lines are
the earliest, least-informed number of the week — before any
practice-participation signal or Wednesday/Thursday injury news has moved
them. This week's waiver claims are not settled yet at 09:08 — they
process tonight — so read `transactions.csv`'s `execution_type`/
`is_pending` fields rather than assuming same-day settlement; the
following morning's `espn-wednesday` pull (see Wednesday, below) is what
actually closes that gap.

**Fantasy strategy implications.** Tuesday's freshly opened spreads/totals
are the baseline for game-script expectations on any waiver pickup you're
considering (`implied_team_total` from `projections.implied_team_totals`)
— a low team total should temper enthusiasm for a bench-and-hope waiver
add even before practice/injury news exists. Tuesday morning is the last
submit/adjust window before tonight's waiver run, not a confirmation
window — use it to finalize claims, then check Wednesday morning for
results.

## Wednesday

**What gets pulled and why.** This is day one of the Wednesday/Thursday/
Friday practice-report window that `data-sources.md` explicitly calls
out: `practice_trajectory` is reconstructed entirely from *our own* daily
Sleeper snapshots and needs three consecutive daily runs (Wed/Thu/Fri) to
fill in — it reads `— / — / —` before that. Running `sleeper` today is the
first of those three required snapshots. An ESPN `--refresh` on
`transactions.csv` at 09:08 now captures the results of last night's
waiver run — this league processes waivers Tuesday night into Wednesday
*(Documented — league setting, per the league manager)*, so this pull is
the first place those settlements land on disk, closing a gap that
otherwise held until the *following* Monday. Nflverse's stat corrections
should be finishing landing today. Player-props markets open
Wednesday–Thursday per the Odds API's market-open table, but no scheduled
job pulls them yet — pulling before Wednesday would just return an empty,
free response (per `odds-budget.md`, this looks identical to "market is
thin" and is explicitly guarded against in `jobs.py:props_primary`).

**Implications of the data.** A single Wednesday practice value in
isolation is not the signal — it's one-third of a trajectory. Don't treat
`DNP` on Wednesday alone as a firm out/doubtful signal; `tier` is derived
from the current snapshot and will update as Thursday/Friday data arrives.
By Wednesday, nflverse's `provisional` flag for last week's games should
finally be settling, but `data-sources.md` is explicit that **Thursday's
read is the first canonical one** — Wednesday is close but not guaranteed
final. On the waiver side, `transactions.csv`'s rows for this week's claims
should now show `is_pending = False` where they cleared — the first time
this pipeline can distinguish a won claim from a pending one for the
current week, rather than seeing that only after the fact next Monday.

**Fantasy strategy implications.** The waiver deadline was last night, not
tonight — this is the first morning to confirm what actually cleared and
know your real bench before making further moves. Note Wednesday's
practice status as a data point, not a decision — wait for Thursday and
Friday before making a start/sit call based on practice participation
alone.

## Thursday

**What gets pulled and why.** Run `nflverse --force` today — this is the
"first canonical" read `data-sources.md` calls out explicitly: stat
corrections land Tuesday/Wednesday, so a Thursday pull is the first one
where `provisional` reliably resolves to `false` for last week's games.
Today is also day two of the Sleeper practice window, and the Odds API's
`props` job runs Thursday (cost ~10–20 credits, per `odds-budget.md`'s
five-job schedule) once player-props markets have had a day to open.

**Implications of the data.** Trust Thursday's nflverse pull as the real
number for last week's snap share, target share, and usage trends — a
Wednesday pull of the same fields could still shift. Sleeper's
`practice_trajectory` is still incomplete (`Wed/Thu/—`) — one more day
needed before it's a complete three-day read. An empty or thin Thursday
props response for a given player just means the market hasn't
consolidated yet for that game, not that no props exist.

**Fantasy strategy implications.** This is the right day to finalize
last-week's usage-trend read (snap_pct_delta, target_share) for
trade/waiver evaluation, since the numbers are now canonical rather than
provisional. For this week's Thursday-night game specifically, don't
finalize that lineup call off a two-day practice trajectory alone — a
short week gives less signal than a full Wed/Thu/Fri read would for
Sunday's games.

## Friday

**What gets pulled and why.** This is the third and final day of the
Sleeper practice window — `practice_trajectory` (`Wed/Thu/Fri`) is now
complete, and `depth_chart_improved`/`depth_chart_promoted` deltas (which
diff against a snapshot up to `--depth-lookback` days prior, default 3)
have their fullest input for the week. The Odds API's `line_movement` job
runs Friday specifically to capture how spreads/totals have shifted since
Tuesday's open, now that the week's injury and practice news has had time
to move markets.

**Implications of the data.** Friday's `practice_trajectory` is the most
complete signal of the week for practice-based availability — treat it as
the primary input to `tier` (`OUT`/`HIGH_RISK`/`COIN_FLIP`/
`LIKELY_PLAYS`/`CLEAR`), not Wednesday's or Thursday's snapshot in
isolation. Line movement since Tuesday reflects the market's own read on
exactly the injury/practice news you've been tracking all week — a
significant move is itself a signal worth cross-checking against your own
read.

**Fantasy strategy implications.** Friday is the natural day to make
start/sit and waiver calls for players whose availability was in question
— the three-day practice trajectory plus Friday's official injury
designations are as complete as the data gets before gameday. Compare
Friday's `line_movement` against your own game-script read: a team total
that moved down sharply since Tuesday is worth weighing before locking in
a bench-and-hope flex play.

## Saturday

**What gets pulled and why.** No Odds API job is scheduled Saturday — the
five-job schedule in `odds-budget.md` has no Saturday slot, deliberately,
since there's no new market event between Friday's `line_movement` and
Sunday's `pre_lock`. nflverse's routine background refresh (the suggested
`9,13,18` cron) continues to run, mainly to catch any late-breaking
practice-squad moves or depth-chart updates, but no new decision-relevant
signal is expected today.

**Implications of the data.** Saturday is a quiet day by design, not a
gap — the practice-report window closed Friday and gameday inactives
aren't out yet. Nothing here should override Friday's read.

**Fantasy strategy implications.** Treat Saturday as prep, not new
information: finalize the lineup decisions built on Friday's complete
practice trajectory and line movement, and hold your final call until
Sunday's `pre_lock` data and any Saturday-night/Sunday-morning inactive-
list news (not covered by any feed in this pipeline) is in.

## Sunday

**What gets pulled and why.** The Odds API's `pre_lock` job runs Sunday
morning (10:30 ET) with **critical** priority — the only job in this
pipeline allowed to draw down the 40-credit reserve — to capture featured
lines and any still-undecided player-prop slots right before kickoff. On
the ESPN side, this is the one day `LIVE_TTL` (300 seconds) actually
matters: `weekly-rosters.csv` and `matchups.csv`'s live fields
(`points_live`, `winner`) need a fresh `--refresh` during the day to
reflect in-progress scoring, since neither view auto-refreshes on its own.

**Implications of the data.** `pre_lock`'s props are the last word before
markets close — this is the most current signal in this pipeline for any
still-undecided flex/DST/prop-driven call. On ESPN, a live matchup's
`points_live`/`winner` will look frozen at whatever they were at your last
`--refresh` — `points_final` will read `0.0` and `winner` will read
`UNDECIDED` for any game still in progress until you refresh again.

**Fantasy strategy implications.** Use `pre_lock`'s undecided-slot props
as the final tiebreaker for any lineup call you were holding open from
Saturday. During the day, refresh ESPN's live views periodically if you
need in-progress score tracking — don't rely on a single morning pull to
reflect afternoon/night-game scoring.

## Known caveats

- **A slot can still be missed, but no longer silently.** The clock moved off
  GitHub precisely because its cron dropped slots without saying so — one
  `health` slot arrived 4h30m late and five consecutive slots never fired at
  all *(Observed — 2026-09-15)*. EventBridge fires at the exact minute and
  dead-letters what it cannot deliver, which raises an alarm, so treat the
  times above as real rather than approximate. A missed slot is re-runnable by
  hand for every feed except Sunday live scoring, which cannot be backfilled.
  See `docs/aws-scheduling.md`.
- **Odds slots do not retry automatically, by design.** Delivery is
  at-least-once, and a duplicate `pre_lock` would spend credits that cannot be
  bought back, so the five metered slots trade automatic retry for the alarm.
  If one fails, it needs a person.
- **A budget-aborted Odds job leaves a stale snapshot in place**, with no
  visible difference on disk from a fresh one — always check
  `captured_at` and `last_run.json`'s `stale` field, never the parquet
  file's mtime, before trusting a day's odds data (`odds-budget.md`'s
  runbook section).
- **ESPN's non-`ttl_for` views never auto-refresh, regardless of day.**
  `matchups.csv`, `transactions.csv`, and `player-pool.csv` are cached
  forever once fetched; every day's guidance above that references
  "refresh" for these views means an explicit `--refresh` flag, not
  something that happens by visiting the right day of the week.
- **An empty Odds API response is free but not informative** — before
  Wednesday, an empty props pull means the market isn't open yet, not
  that nothing exists; treat it accordingly rather than as a real signal.
- **The Odds `results` job cannot see Monday night's game.** It runs
  Monday 09:38 with `daysFrom=3`, a window that closes before that
  night's game has been played; that game's result lands in the
  following week's `results` pull instead. This is an accepted
  limitation of the job's timing, not a bug.
- **Before the Wednesday 09:08 ESPN pull was added, waiver settlements
  were invisible on disk from Tuesday night until the following Monday
  09:08** — a six-day gap, since Tuesday's own 09:08 run happens roughly
  twelve hours before that night's processing. `espn-wednesday` closes it
  by pulling `transactions.csv` the morning after the run. One residual
  limit: a *pending* claim seen Tuesday and the *settled* row seen
  Wednesday are the same underlying row mutating vendor-side, and
  `transactions.csv` is one of the non-`ttl_for` views cached forever
  once fetched (above) — so Wednesday's value depends on that pull's
  `--refresh` actually running.
