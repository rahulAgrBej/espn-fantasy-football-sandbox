# Availability watchlist -- 2026 week 2

**Covers** Wed 2026-09-16 -- the first of week 2's three practice days
**Week 2** Tue 2026-09-15 03:00 - Tue 2026-09-22 03:00 ET
**Rendered** Wed 2026-09-16 10:03 ET

## Freshness
- sleeper: 2026-09-16 08:11:31 ET
- nflverse: 2026-09-16 09:24:08 ET
- espn: 2026-09-15 14:10:04 ET
- odds: 2026-09-15 18:42:00 ET

## Decisions due
**None binding today.** This is a contingency list, not an action: one practice day is one-third of the trajectory Friday's lineup lock will read.
0 of 9 starters are on the watchlist as of this morning's snapshot.
_Waiver-deadline note: this league's deadline was last night (Tuesday into Wednesday), not tonight -- this morning's ESPN pull is the first settled read of last night's run._

## Waiver outcomes
0 claimed by us, 0 claimed by other teams, 0 newly available.
_2 other transaction(s) in this window were not FREEAGENT/WAIVER ADD/DROP rows and are excluded above._

### Claimed by us
_(none)_

### Claimed by other teams
_(none)_
### Newly available
_(none)_

## Watchlist
No starter is OUT, HIGH_RISK or COIN_FLIP on today's read.

## Practice report -- full roster
| player | tier | practice | trajectory (W/T/F) | depth order | Sleeper match |
|---|---|---|---|---|---|
| Caleb Williams | CLEAR | -- | — / — / — | 1.0 | yes |
| Kenneth Walker III | CLEAR | -- | — / — / — | 1.0 | yes |
| Cam Skattebo | CLEAR | -- | — / — / — | 1.0 | yes |
| Rhamondre Stevenson | CLEAR | -- | — / — / — | 1.0 | yes |
| CeeDee Lamb | CLEAR | -- | — / — / — | 1.0 | yes |
| Malik Nabers | CLEAR | -- | — / — / — | 1.0 | yes |
| Mark Andrews | CLEAR | -- | — / — / — | 1.0 | yes |
| Seahawks D/ST | CLEAR | -- | — / — / — | -- | yes |
| Tyler Loop | CLEAR | -- | — / — / — | 1.0 | yes |
| DK Metcalf | CLEAR | -- | — / — / — | 1.0 | yes |
| Chuba Hubbard | CLEAR | -- | — / — / — | 1.0 | yes |
| RJ Harvey | CLEAR | -- | — / — / — | 2.0 | yes |
| De'Zhaun Stribling | OUT | -- | — / — / — | 10.0 | yes |
| Tre Tucker | CLEAR | -- | — / — / — | 1.0 | yes |
| Jordan Love | CLEAR | -- | — / — / — | 1.0 | yes |

## Depth chart moves
| player | current order | improved | promoted | days used |
|---|---|---|---|---|
| Rhamondre Stevenson | 1.0 | True | False | 2 |

## Drop candidates
_None by design._ Dropping on one day of practice data discards a player before the signal that would justify it exists. Tuesday's waiver report owns the drop list.

## What this report cannot see
- One practice day is one-third of a trajectory. `tier` is derived from today's snapshot alone and will move as Thursday's and Friday's practice reports land.
- `practice_trajectory` reads `Wed / -- / --` today by construction, not because a feed failed. A `-- / -- / --` row means no Sleeper match or no snapshot on that day, and that gap is permanent -- Sleeper publishes no history to backfill from.
- `depth_chart_days_used` is the gap actually used, which falls back to the oldest available snapshot when none exists at exactly the lookback distance. Read it before trusting either depth-chart delta.
- `pos_rank` is deliberately absent. It comes from nflverse `depth_charts`, an append-only log with no week column, so reading it as this week's depth is wrong.
- `report_status` is null both when the nflverse injuries feed is unavailable and when a player carries no designation; those two are indistinguishable in that column.
- No drop candidates today. Dropping on one day of practice data discards a player before the signal that would justify it exists.
- This league's waiver deadline was last night (Documented -- league setting, per the league manager), not tonight. This morning's ESPN pull captures transactions.csv in its settled state, though this report does not itself render waiver rows -- see Tuesday's waiver report.
- "Newly available" below is a render-time snapshot of this week's free-agent pool, not a guarantee -- a listed player can be claimed before this report is read.
- Waiver outcomes below share the same {week - 1, week} transactions.csv window waivers.settlements() uses.
