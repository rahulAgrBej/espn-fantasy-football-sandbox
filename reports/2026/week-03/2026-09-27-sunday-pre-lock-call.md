# Final lock -- 2026 week 3

**Covers** week 3's Sunday lineup lock, before 13:00 ET
**Week 3** Tue 2026-09-22 03:00 - Tue 2026-09-29 03:00 ET
**Rendered** Sun 2026-09-27 11:30 ET

**Generated Sun 2026-09-27 11:30 ET -- 89 minutes to the first Sunday kickoff (13:00 ET). After kickoff this report is history.**

## Freshness
- sleeper: 2026-09-27 08:11:33 ET
- nflverse: 2026-09-27 09:24:09 ET
- espn: 2026-09-27 11:30:55 ET
- odds: 2026-09-27 10:38:37 ET
- odds (`pre_lock`): **insufficient data** -- pre_lock's last run is marked stale: run budget exceeded: spent_in_run=25 + est=3 > run_budget=25
_The `odds:` line above is `loaders._odds_freshness`, hardcoded to the `slate_context` job -- it reads Tuesday's capture. The `pre_lock` line is this report's own read._

## Decisions due
**The final lock, before 13:00 ET.**

- 89 minutes remain (source: nflverse schedules).
- 0 slot(s) still undecided this morning.
- 0 starter(s) read `OUT`.

## Did `pre_lock` actually run
**insufficient data** -- pre_lock's last run is marked stale: run budget exceeded: spent_in_run=25 + est=3 > run_budget=25

## Slots still undecided as of this morning
_Recomputed by `friday.recommended_lineup`/`friday.held_open_slots` against today's data. Nothing persists Friday's own list; these agree with it whenever no input has moved._

No slot is undecided -- every recommended starter is either clear or clearly better.

## Starters whose tier reads `OUT`
_(none)_

## The lineup you are locking
| slot | player | projected | tier | prop points |
|---|---|---|---|---|
| TE | Mark Andrews | 10.2 | CLEAR | insufficient data |
| D/ST | Seahawks D/ST | 7.3 | CLEAR | insufficient data |
| K | Tyler Loop | 8.7 | CLEAR | insufficient data |
| QB | Jordan Love | 17.3 | CLEAR | insufficient data |
| RB | Kenneth Walker III | 21.0 | CLEAR | insufficient data |
| RB | Chuba Hubbard | 14.4 | CLEAR | insufficient data |
| WR | CeeDee Lamb | 17.3 | CLEAR | insufficient data |
| WR | Malik Nabers | 14.4 | CLEAR | insufficient data |
| RB/WR | Cam Skattebo | 14.0 | CLEAR | insufficient data |

## Pre-lock featured lines
insufficient data -- pre_lock's last run is marked stale: run budget exceeded: spent_in_run=25 + est=3 > run_budget=25

## Pre-lock props -- still-undecided slots only
insufficient data -- pre_lock's last run is marked stale: run budget exceeded: spent_in_run=25 + est=3 > run_budget=25

## What this report cannot see
- Official inactives drop roughly 90 minutes before kickoff and appear in no feed this pipeline touches -- a report generated at 11:30 ET cannot see them, and nothing above substitutes for checking them yourself before kickoff.
- The undecided slots above are recomputed against this morning's data by the same `friday.recommended_lineup`/`friday.held_open_slots` this week's Friday report ran. Nothing persists Friday's own computation. The two agree whenever no input has moved since Friday; when one has, this morning's answer is the more current one, not a discrepancy to reconcile.
- `pre_lock` is the only odds job permitted to draw down the reserve; `docs/odds-budget.md` owns those rules. This report neither enforces nor estimates that budget -- it only verifies the job ran.
- `pre_lock` pulls props with `commence_after=now` at 10:38, so a game already kicked off by then carries no props here -- an absence in the market table is not evidence of no quote. The capture carries no kickoff time and `props_by_capture` drops `event_id`, so a game kicking off between 10:38 and this render can still appear under a heading that says "still undecided".
- A player quoted on one market has an understated market total -- the markets column says how many were summed.
- The kickoff clock reads nflverse's `gameday`/`gametime` as an ET wall clock, which nflverse does not publish a zone for (Inferred). When the slate cannot be read at all the deadline shown is the spec's 13:00 ET default, labelled as such.
- Ranking undecided slots by the `pre_lock` consensus rather than ESPN's projection is a chosen rule, not a fitted one -- no report in this repo is ever scored against what actually happened.
- This filename carries the render date, not the covered one, same convention as every other report.
- `pre_lock` could not be verified -- pre_lock's last run is marked stale: run budget exceeded: spent_in_run=25 + est=3 > run_budget=25. Every market figure above is Friday's data or none.
- The `pre_lock` props could not be read -- pre_lock's last run is marked stale: run budget exceeded: spent_in_run=25 + est=3 > run_budget=25.
