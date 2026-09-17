# Usage and market -- 2026 week 2

**Covers** week 2's Thursday-night start/sit; canonical usage below reviews week 1
**Week 2** Tue 2026-09-15 03:00 - Tue 2026-09-22 03:00 ET
**Rendered** Thu 2026-09-17 12:54 ET

## Freshness
- sleeper: 2026-09-17 08:11:32 ET
- nflverse: 2026-09-17 12:53:37 ET
- espn: 2026-09-17 12:54:58 ET
- odds: 2026-09-15 18:42:00 ET

## Decisions due
**The Thursday-night start/sit is binding at kickoff** -- the only hard call today.
- DET @ BUF, 20:15 ET

No rostered player is on tonight's two teams.

## Canonical usage -- week 1
| player | pos | offense_pct | snap_pct_delta_1w | snap_pct_delta_3w | snap_pct_trend | targets | target_share | air_yards_share | wopr | targets_per_snap |
|---|---|---|---|---|---|---|---|---|---|---|
| Mark Andrews | TE | 0.72 | -- | -- | -- | 6 | 0.25 | 0.164 | 0.489 | 0.122 |
| D.K. Metcalf | WR | 0.96 | -- | -- | -- | 10 | 0.27 | 0.501 | 0.756 | 0.156 |
| Jordan Love | QB | 1.0 | -- | -- | -- | 0 | 0.0 | 0.0 | 0.0 | 0.0 |
| CeeDee Lamb | WR | 0.81 | -- | -- | -- | 8 | 0.267 | 0.291 | 0.604 | 0.17 |
| Chuba Hubbard | RB | 0.71 | -- | -- | -- | 3 | 0.088 | 0.0 | 0.132 | 0.062 |
| Chuba Hubbard | RB | 0.71 | -- | -- | -- | 3 | 0.088 | 0.0 | 0.132 | 0.062 |
| Chuba Hubbard | RB | 0.71 | -- | -- | -- | 3 | 0.088 | 0.0 | 0.132 | 0.062 |
| Rhamondre Stevenson | RB | 0.85 | -- | -- | -- | 6 | 0.194 | -0.027 | 0.271 | 0.1 |
| Kenneth Walker III | RB | 0.68 | -- | -- | -- | 6 | 0.24 | -0.015 | 0.349 | 0.128 |
| Tre Tucker | WR | 0.71 | -- | -- | -- | 4 | 0.138 | 0.221 | 0.362 | 0.083 |
| Malik Nabers | WR | 0.52 | -- | -- | -- | 9 | 0.31 | 0.42 | 0.76 | 0.25 |
| Caleb Williams | QB | 1.0 | -- | -- | -- | 0 | 0.0 | 0.0 | 0.0 | 0.0 |
| Cam Skattebo | RB | 0.61 | -- | -- | -- | 0 | 0.0 | 0.0 | 0.0 | 0.0 |
| RJ Harvey | RB | 0.51 | -- | -- | -- | 4 | 0.148 | -0.239 | 0.055 | 0.154 |
| RJ Harvey | RB | 0.51 | -- | -- | -- | 4 | 0.148 | -0.239 | 0.055 | 0.154 |
| Caleb Douglas | WR | 0.91 | -- | -- | -- | 7 | 0.259 | 0.314 | 0.609 | 0.137 |

## Market -- prop-derived points
**insufficient data** -- no player_props.parquet on disk

## Divergence -- prop points vs ESPN projection
No player's prop-derived total diverges from ESPN's projection by 25% or more.
_0 player(s) could not be compared -- zero or missing ESPN projection._

## Swap candidates
No starter's one-week snap share fell against a bench player trending the other way.

## Drop candidates
_None today._ The waiver deadline was Tuesday night; a mid-week drop on two practice days trades a real bye-week problem for a marginal add.

## Practice report -- Wed / Thu
| player | trajectory (W/T/--) |
|---|---|
| Caleb Williams | — / — / — |
| Kenneth Walker III | — / — / — |
| Cam Skattebo | — / — / — |
| Rhamondre Stevenson | — / — / — |
| CeeDee Lamb | — / — / — |
| Malik Nabers | — / — / — |
| Mark Andrews | — / — / — |
| Seahawks D/ST | — / — / — |
| Tyler Loop | — / — / — |
| DK Metcalf | — / — / — |
| Chuba Hubbard | — / — / — |
| RJ Harvey | — / — / — |
| Tre Tucker | — / — / — |
| Jordan Love | — / — / — |
| Caleb Douglas | — / — / — |

## What this report cannot see
- The Thursday-night practice trajectory is two days deep (Wed/Thu), not three -- a short week gives genuinely less signal than Sunday's players will have by Friday.
- The market section's `fantasy_points` sums whatever markets this book actually quoted for a player -- a player quoted on only one market is understated relative to one quoted on several; see `markets` in the table.
- Divergence between prop points and ESPN's own projection is a tiebreaker to investigate, never an override -- the two numbers are built from different inputs and disagreement is expected.
- No drop candidates today. The waiver deadline was Tuesday night; a mid-week drop on two practice days trades a real bye-week problem for a marginal add.
- Report filenames carry the render date and week, not the covered one -- the usage section above covers week - 1 while this file is filed under week.
- The market section could not be read -- no player_props.parquet on disk.
- PROJECTION_DIVERGENCE_PCT (25%) is chosen, not fitted.
