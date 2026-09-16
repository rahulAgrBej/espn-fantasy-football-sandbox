# Data sources

Four independent feeds land in this pipeline, and each refreshes on a completely
different clock: ESPN's undocumented fantasy API, Sleeper's free player API,
nflverse's GitHub release assets, and The Odds API's metered betting-market
feed. This doc answers one question per field: *how stale can the number in
front of you be?*

The fourth feed is a different kind of exception to that question. ESPN,
Sleeper and nflverse are all free to re-check, so their freshness ceiling is a
TTL or an ETag -- a cadence. The Odds API is metered (500 credits per billing
period, no historical endpoint), so its freshness ceiling is a *budget*: a
value is exactly as fresh as the last job that had credit left to run, not as
fresh as the clock says it could be. See `docs/odds-budget.md` for the credit
invariant itself; this document still covers its field-level cadence below.

## How to read this

Each source gets an access summary (base URL, auth, retry policy, what governs
freshness) followed by one table per output artifact — one row per column. Two
cadence columns appear throughout:

- **Upstream release cadence** — how often the vendor itself produces a new value.
- **Our ingest** — how often *this codebase* actually pulls that value down, which
  is frequently slower than the vendor's own clock.

Both columns are left blank (`—`) wherever a field simply inherits the cadence
stated in that table's lead-in paragraph; they're only filled in per-row where a
field's freshness genuinely diverges from its neighbors.

Every cadence claim is sourced one of three ways, and marked accordingly:

- **Observed** — measured from artifacts on disk (e.g. the real `last_updated`
  stamps in `data/nflverse/manifest.json`).
- **Documented** — asserted by the vendor or by a module docstring in this repo
  (e.g. Sleeper's "call at most once a day" ask).
- **Inferred** — deduced from how the scoring-period/schedule model works, not
  published anywhere.

This marking matters most for ESPN, which publishes no cadence contract at all —
nothing below claims a confident refresh interval for an ESPN field that hasn't
actually been measured.

---

## ESPN

**Access.** Base `https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl`, an
undocumented internal API with no versioning guarantee — the host itself moved in
April 2024 with no announcement. League views (`mTeam`, `mRoster`,
`mMatchupScore`, `mDraftDetail`, `mTransactions2`, …) need `ESPN_S2` + `SWID`
session cookies from a logged-in browser; `kona_player_info` (via
`leaguedefaults/3`) and `chui_default_platformsettings` are public. Rate limits
are undocumented. Retries with backoff on `429`/`5xx`, four attempts.

**Freshness mechanism.** `cache.ttl_for` (`espn_ff/cache.py:75`) says a closed
scoring period should be cached forever and anything else — the live week, or a
view with no single week at all — should get `LIVE_TTL = 300` seconds
(`ttl_for(scoring_period=None, ...)` returns `LIVE_TTL` unconditionally). Every
`get_league`/`get_player_pool` call site in `cli.py` and `odds/jobs.py` now
passes an explicit `ttl` — the weekly-roster fetch (`mRoster`, backing
`weekly-rosters.csv` and the `team` command) via `ttl_for(week, current)`, and
every league-wide view — `mTeam`/`mStandings` (`teams.csv`), `mMatchupScore`
(`matchups.csv`), `mDraftDetail` (`draft.csv`), `mTransactions2`
(`transactions.csv`), and `kona_player_info` (`player-pool.csv`) — via
`ttl_for(None, current)` *(Documented — the fetch sites themselves)*. A closed
week's own cache never expires either way; what changed is that a league-wide
view can no longer be served indefinitely once it's more than 5 minutes stale.
In practice: a live matchup's score, an in-flight transaction, or a nudge in
`percent_owned` refreshes itself on the next call more than `LIVE_TTL` seconds
after the last one, with no `--refresh` needed. `--refresh` still exists and is
still what every scheduled workflow passes (`.github/workflows/espn.yml`,
`docs/automation.md`) — it forces an immediate re-fetch regardless of TTL,
which matters for Sunday's 30-minute live-scoring cadence (comfortably past 300
seconds anyway, so this is now belt-and-suspenders there) and for anyone who
wants guaranteed-fresh data sooner than the TTL would otherwise allow.

The one exception is `current_scoring_period()` itself (`espn_ff/client.py`),
which every `--week`-defaulting command depends on. Its underlying fetch
(`get_platform_settings`, the `chui_default_platformsettings` view) also has no
`ttl`, but the *value* is no longer read straight off that cache-forever
payload's `currentScoringPeriod.id`. Instead it resolves "what week is it" by
comparing `now` against the `scoringPeriods` calendar (`startDate`/`endDate` per
week, published for the whole season up front) already sitting in that same
cached payload — a local comparison, no network. The underlying platform-
settings fetch only refetches when the calendar can no longer answer the
question: no cached copy yet, `now` falls outside every published window, or a
week boundary has been crossed since the cache was last written — and even then
only once per `LIVE_TTL` (300s) window, so a boundary ESPN hasn't honoured yet
doesn't trigger a refetch on every single call. A failed refetch warns and
falls back to the stale calendar's answer rather than raising. Targeted re-pull
for one suspect week is `--refresh-weeks` (e.g. `--refresh-weeks 3` or `1-4`),
which bypasses the cache for week-scoped fetches only — a league-wide view
older than `LIVE_TTL` refetches on its own, but forcing it sooner still needs
plain `--refresh`.

### `teams.csv` — `teams.teams_frame` (13 fields)

Written by `export`; standings fields (`wins`, `losses`, `points_for`, …)
reflect ESPN's own running total, refetched automatically once the cached
copy is more than `LIVE_TTL` (300s) old, or immediately with `--refresh`.

| Field | Meaning | Upstream release cadence | Our ingest |
|---|---|---|---|
| `team_id` | ESPN's internal team id | — | — |
| `team_name` | Resolved team name (2023+ single field; older leagues join location+nickname) | — | — |
| `abbrev` | Team abbreviation | — | — |
| `owner` | Owner name(s), joined from `members[]`; comma-separated for co-owned teams | — | — |
| `wins` / `losses` / `ties` | Season record | — | — |
| `points_for` / `points_against` | Season cumulative points | — | — |
| `percentage` | Win percentage | — | — |
| `playoff_seed` | Current playoff seed | — | — |
| `draft_day_projected_rank` | ESPN's pre-season projected standing | Set once, pre-season | Static after draft day |
| `waiver_rank` | Current waiver priority | — | — |

### `scoring-rules.csv` — `settings.scoring_frame` (5 fields)

League configuration, set at league creation. Effectively static in-season.

| Field | Meaning | Upstream release cadence | Our ingest |
|---|---|---|---|
| `stat_id` | ESPN stat id | League creation | Cached forever |
| `stat_abbrev` | Resolved via the 235-entry stat dictionary (`constants.stat_dictionary`, pulled from the public platform-settings endpoint) | — | — |
| `points` | Points awarded per unit of the stat | — | — |
| `is_reverse` | Whether more of the stat is worse (e.g. turnovers) | — | — |
| `points_overrides` | Position-specific point overrides, if any | — | — |

### `roster-slots.csv` — `settings.roster_slots_frame` (3 fields)

League configuration, same as scoring rules — static in-season.

| Field | Meaning | Upstream release cadence | Our ingest |
|---|---|---|---|
| `slot_id` | Lineup slot id | League creation | Cached forever |
| `slot` | Resolved slot label (`constants.slot`, e.g. `RB/WR`, `FLEX`, `Bench`) | — | — |
| `count` | Number of that slot in a starting lineup | — | — |

### `weekly-rosters.csv` — `rosters.rosters_frame` (15 fields)

One row per (team, week, player). `mRoster` returns the roster *as of* the
requested scoring period, so **this is the one artifact where `ttl_for` is
actually applied**: a closed week's rows are cached forever (immutable), and the
current week's rows get a 300-second TTL before the next `pull`/`export`
re-fetches them.

| Field | Meaning | Upstream release cadence | Our ingest |
|---|---|---|---|
| `season` / `week` | Season and scoring period this roster snapshot is for | — | — |
| `team_id` / `team_name` | Owning team | — | — |
| `player_id` / `player_name` | Player | — | — |
| `position` / `pro_team` | Resolved position and NFL team | — | — |
| `lineup_slot_id` / `lineup_slot` | Slot the player occupied that week | — | — |
| `started` | `True` if `lineup_slot_id` is not Bench/IR/ER (`constants.NON_SCORING_SLOTS`) | — | — |
| `injury_status` | ESPN's own status string as of that fetch | Closed week: frozen | Live week: `LIVE_TTL` (300s) |
| `acquisition_type` | How the player joined the roster (`DRAFT`/`ADD`/`TRADE`/`WAIVER`) | — | — |
| `points` / `projected` | Actual/projected points for that week (see the `stats[]` selection trap below) | Closed week: frozen | Live week: `LIVE_TTL` (300s) |

### `matchups.csv` — `matchups.matchups_frame` (16 fields)

One row per (week, matchup, side). Fetched via `mMatchupScore`, wired to
`ttl_for(None, current)` — see the freshness-mechanism note above. Once
cached, a live matchup's score refreshes on its own after `LIVE_TTL` (300s);
`--refresh` still forces it sooner.

| Field | Meaning | Upstream release cadence | Our ingest |
|---|---|---|---|
| `season` / `week` / `matchup_id` / `playoff_tier` / `side` | Identifiers | — | — |
| `team_id` / `team_name` | Team on this side | — | — |
| `points` | `points_live` while the week is open, else `points_final` (the safe field to read) | — | — |
| `points_final` | `totalPoints`; **`0.0` while the week is in progress** — not yet a real number | Closes when the week does | `LIVE_TTL` (300s); `--refresh` for sooner |
| `points_live` | `totalPointsLive`; the actual running score during a live week | Updates continuously server-side, undocumented cadence *(Inferred)* | `LIVE_TTL` (300s); `--refresh` for sooner |
| `projected` | ESPN's own week projection | — | — |
| `opponent_id` / `opponent_name` / `opponent_points` | Mirror of the above for the other side | — | — |
| `winner` | `HOME`/`AWAY`/`TIE`/**`UNDECIDED`** while the week is open — authoritative, not inferred from the two point totals | Closes when the week does | `LIVE_TTL` (300s); `--refresh` for sooner |
| `result` | `W`/`L`/`T` derived from `winner`; `None` while `winner` is `UNDECIDED` | Closes when the week does | `LIVE_TTL` (300s); `--refresh` for sooner |

### `draft.csv` — `draft.draft_frame` (15 fields)

Written once, on draft day; frozen after.

| Field | Meaning | Upstream release cadence | Our ingest |
|---|---|---|---|
| `season` / `overall_pick` / `round` / `round_pick` | Pick identifiers | — | — |
| `team_id` / `team_name` | Drafting team | — | — |
| `drafted_by` | Member name who made the pick | — | — |
| `player_id` / `player_name` | Player drafted | — | — |
| `lineup_slot` | Slot the pick was assigned to | — | — |
| `bid_amount` | Auction-draft dollar amount (`None` for snake drafts) | — | — |
| `nominating_team_id` | Auction nominator | — | — |
| `keeper` / `reserved_for_keeper` | Keeper-league flags | — | — |
| `auto_draft_type` | Decoded from `autoDraftTypeId` (`manual`/`auto`/`auto_offline`/`autopick`) | — | — |

### `transactions.csv` — `transactions.transactions_frame` (23 fields)

One row per transaction *item* (a trade moving several players becomes several
rows). Appends in near-real-time as managers act; existing rows never change,
and — like matchups — the *fetch* is wired to `ttl_for(None, current)`, so a
transaction that posted after your last `pull`/`export` appears on its own
once the cached copy is more than `LIVE_TTL` (300s) old, sooner with
`--refresh`.

| Field | Meaning | Upstream release cadence | Our ingest |
|---|---|---|---|
| `season` / `transaction_id` / `scoring_period` | Identifiers | — | — |
| `type` / `item_type` | Transaction type and item type | — | — |
| `status` / `execution_type` / `is_pending` | Transaction state | — | — |
| `acting_team_id` / `acting_team` / `acting_member` | Team/member who acted | — | — |
| `player_id` / `player_name` | Player involved | — | — |
| `from_team_id` / `from_team` | Source team; team id `0` (free agency) is normalized to `None` | — | — |
| `to_team_id` / `to_team` | Destination team; same `0` → `None` normalization | — | — |
| `from_slot` / `to_slot` | Lineup slot before/after; slot `-1` ("no slot") normalized to `None` | — | — |
| `bid_amount` | Waiver bid amount, if applicable | — | — |
| `is_keeper` | Keeper flag on this item | — | — |
| `proposed_date_ms` / `proposed_date` | Raw epoch-ms and parsed timestamp of the transaction | — | — |

### `player-pool.csv` — `players.players_frame` (16 fields)

Built from the public `kona_player_info` view, via `get_player_pool`, wired to
`ttl_for(None, current)` like the other league-wide views above: refetches on
its own once the cached copy is more than `LIVE_TTL` (300s) old, sooner with
`--refresh`.

| Field | Meaning | Upstream release cadence | Our ingest |
|---|---|---|---|
| `player_id` / `player_name` | Player | — | — |
| `position` / `pro_team` | Resolved position and NFL team | — | — |
| `active` / `injured` / `injury_status` | ESPN's own player-status fields as of the fetch | — | — |
| `on_team_id` | Fantasy team that owns this player, if any | — | — |
| `percent_owned` | League-wide rostered percentage | Moves continuously, vendor-side, undocumented cadence *(Inferred)*; **has no "final" state, unlike everything else in this file** | `LIVE_TTL` (300s); only as fresh as the last fetch within that window |
| `percent_started` | League-wide started percentage | Same as `percent_owned` | Same as `percent_owned` |
| `eligible_slots` | Comma-joined list of slots this player is eligible for | — | — |
| `season_points` / `season_projected` | Season-to-date actual/projected points (see the `stats[]` trap) | — | — |
| `week` / `week_points` / `week_projected` | Present when exported with a specific week (`export` passes the current scoring period) | — | — |

**The `stats[]` selection trap** (all `points`/`projected` fields above):
a player's `stats[]` array mixes seasons, sources, and split types in one flat
list. `espn_ff/stats.py` keys every selector on `seasonId` + `scoringPeriodId` +
`statSourceId` (`0` actual / `1` projected) + `statSplitTypeId` — filtering on
fewer fields silently returns the wrong season's row (verified live: 128 of 280
sampled players resolved to the *2025* row before this fix).

---

## Sleeper

**Access.** `https://api.sleeper.app`, free and unauthenticated, no per-league
scoping — it only ever returns the NFL-wide player pool. Three endpoints used:
`GET /v1/players/nfl` (~15 MB, unpaginated, the whole player pool), and
`GET /v1/players/nfl/trending/add` / `.../drop` (`lookback_hours=24`, `limit`
from `--trending-limit`, default 25). Retries with backoff on `429`/`5xx`, four
attempts.

**Freshness mechanism.** *Documented*: Sleeper asks that `/v1/players/nfl` be
called **at most once a day**. `espn_ff/sleeper/snapshots.py:fetch_players` is
the only code path allowed to call it, and it short-circuits if today's slim CSV
already exists, unless `--refresh` is passed. A guard (`MIN_ACTIVE_PLAYERS =
2000`) protects against a bad response silently overwriting a good snapshot: a
failed fetch, or a response with fewer than 2,000 active players, leaves
yesterday's snapshot untouched and writes `last_run.json` with `stale: true` and
a `reason` — it never falls through to "no injuries anywhere" for the week.

The cadence distinction that matters most here: **`practice_participation` is a
single current value with no history from Sleeper.** `practice_trajectory` is
reconstructed entirely from *our own* daily snapshot store, so it needs three
consecutive daily `sleeper` runs to fill in (`Wed / Thu / Fri`) and reads
`— / — / —` (or partial) before that. Same story for `depth_chart_improved` /
`depth_chart_promoted` / `depth_chart_days_used`: they diff against a prior
snapshot found within `--depth-lookback` days (default 3), and are `null` on a
cold store. Their usefulness is a function of *our run history*, not of Sleeper's
freshness — that's a property of the `Our ingest` column, not the upstream one.

`trending_add` / `trending_add_count` are alerting context only —
`espn_ff/sleeper/signals.py:8` is explicit that this must never feed a score or a
sort.

### `data/out/dd-mm-yyyy-sleeper-status.csv` — the derived output (15 fields)

Built by `status`, **no network** — reads only snapshots already on disk.

| Field | Meaning | Upstream release cadence | Our ingest |
|---|---|---|---|
| `sleeper_id` | Sleeper's player id | — | — |
| `espn_player_id` | Resolved via `player_id_map.csv` (may be null if unmatched) | — | — |
| `full_name` / `team` / `position` | From today's slim snapshot | Documented: once/day upstream | Once/day, gated by `sleeper` |
| `injury_status` | Sleeper's current injury designation | Documented: once/day upstream | Once/day, gated by `sleeper` |
| `practice_participation` | Sleeper's single current value (`DNP`/`LP`/`FP`, etc.) — no history from the vendor | Documented: once/day upstream | Once/day, gated by `sleeper` |
| `tier` | Derived: one of `OUT`, `HIGH_RISK`, `COIN_FLIP`, `LIKELY_PLAYS`, `UNKNOWN`, `CLEAR` (`sleeper_signals.availability_tier`) | n/a — derived | Recomputed every `status` run from the latest snapshot |
| `practice_trajectory` | `Wed / Thu / Fri` string reconstructed from our own snapshot history | n/a — no vendor history exists | Needs 3 consecutive daily `sleeper` runs; `—` for any day not yet snapshotted |
| `depth_chart_order` | Sleeper's current depth-chart slot | Documented: once/day upstream | Once/day, gated by `sleeper` |
| `depth_chart_improved` / `depth_chart_promoted` | Derived by diffing against a snapshot `--depth-lookback` days prior (default 3) | n/a — derived | `null` on a cold store (no prior snapshot yet) |
| `depth_chart_days_used` | Actual day-gap used for that diff (falls back to the oldest available snapshot if none exists at exactly `--depth-lookback` days) | n/a — derived | Varies run to run; read this before trusting the delta fields |
| `trending_add` / `trending_add_count` | Community add-count flag/value from `/trending/add`; **display-only, never scored or sorted on** | Documented: 24h lookback window, refetched whenever `sleeper` runs | Once/day, gated by `sleeper` |

### `data/sleeper/slim/YYYY-MM-DD-players.csv` — the retained daily snapshot (17 fields, `snapshots.SLIM_COLUMNS`)

Not in `data/out/`, but the actual unit of retention — every derived signal
above reads from this. `KEEP_SLIM = 10`: the 10 most recent daily snapshots are
kept, older ones pruned after each successful `sleeper` run.

| Field | Meaning | Upstream release cadence | Our ingest |
|---|---|---|---|
| `sleeper_id` | Sleeper's player id | — | — |
| `espn_id` | Sleeper's own cross-reference to an ESPN id, where populated | Documented: once/day | Once/day, gated by `sleeper` |
| `gsis_id` | Sleeper's own cross-reference to nflverse's id | Documented: once/day | Once/day, gated by `sleeper` |
| `full_name` | Resolved from `full_name`, else `first_name` + `last_name` | — | — |
| `team` / `position` | Current NFL team / position | — | — |
| `depth_chart_position` / `depth_chart_order` | Current depth-chart entry | — | — |
| `injury_status` / `injury_body_part` / `injury_notes` | Current injury detail | — | — |
| `practice_participation` | Current practice designation, single value | — | — |
| `status` | Sleeper roster status (e.g. Active, Inactive) | — | — |
| `active` | Boolean active flag | — | — |
| `number` | Jersey number | — | — |
| `years_exp` | Years of NFL experience | — | — |
| `fetched_at` | Epoch seconds this snapshot was pulled — the actual freshness stamp for every row in the file | — | — |

Row selection note: rows that are both inactive *and* teamless are dropped
before writing (retired/practice-squad noise, most of the raw file and none of
what matters here).

### `data/sleeper/trending-YYYY-MM-DD.csv` (3 fields)

| Field | Meaning | Upstream release cadence | Our ingest |
|---|---|---|---|
| `player_id` | Sleeper id | Documented: 24h rolling lookback | Once/day, gated by `sleeper` |
| `count` | Raw add/drop count within the lookback window | — | — |
| `kind` | `add` or `drop` | — | — |

### `data/sleeper/player_id_map.csv` (3 fields, `ids.MAP_COLUMNS`)

| Field | Meaning | Upstream release cadence | Our ingest |
|---|---|---|---|
| `sleeper_id` | Sleeper id | n/a | Rebuilt every `sleeper` run, except... |
| `espn_player_id` | Resolved ESPN id — by direct `espn_id` match, then D/ST team+position, then normalized name+team+position | n/a | ...rows with `source == "manual"` are hand-edits that survive every re-run untouched |
| `source` | `espn_id` / `name` / `manual` | n/a | — |

### `data/sleeper/last_run.json`

The staleness record itself.

| Field | Meaning | Upstream release cadence | Our ingest |
|---|---|---|---|
| `fetched_at` | Epoch seconds of the last `sleeper` run (successful or not) | — | Written every `sleeper` run |
| `player_count` | Row count of that run's slim frame, or `null` on a failed/guarded run | — | — |
| `stale` | `true` if the fetch failed or fell below `MIN_ACTIVE_PLAYERS` (2000) — the snapshot on disk is a fallback to the last good day | — | — |
| `reason` | Human-readable reason when `stale` is `true` | — | — |

---

## nflverse

**Access.** GitHub release assets at
`https://github.com/nflverse/nflverse-data/releases/download/{tag}/{filename}`.
No auth. Freshness is checked via each release tag's `timestamp.json` plus an
HTTP `If-None-Match` conditional GET — **explicitly not a TTL**. Verified live:
a stored ETag reliably gets a `304` with zero body bytes through GitHub's
redirect to signed Azure blob storage, so the download-avoidance design is not
speculative. There's also a plain-GET, no-ETag fallback for dynastyprocess's
`db_playerids.csv` (id crosswalk backfill only, best-effort — any failure
returns an empty frame rather than raising).

This layer does **not** use `nflreadr` or `nfl_data_py` — it pulls release
assets over HTTP directly, so there's no `load_snap_counts`-style call to point
to. Play-by-play is deferred, not ingested (see Deferred, below).

### Feed-level cadence (the exception to the field-level rule)

The six release assets have genuinely different clocks, and every downstream
field below inherits its cadence from exactly one of them — so this table leads
with the feed, not a column. Observed `last_updated` timestamps below are a
**single day's snapshot** (2026-09-14) of `data/nflverse/manifest.json`, not a
schedule; they exist to show the feeds settling across the day, which they
visibly do — not to promise a repeatable interval.

| Feed | Tag / file | Observed `last_updated` (2026-09-14) | Rows | Optional? |
|---|---|---|---|---|
| `players` | `players.parquet` | 10:39 EDT | 24,820 | No |
| `snap_counts` | `snap_counts_2026.parquet` | 08:17 EDT | 1,397 | No |
| `stats_player` | `stats_player_week_2026.parquet` | 11:56 EDT | 1,041 | No |
| `schedules` | `games.parquet` | 15:36 EDT | 7,548 | No |
| `injuries` | `injuries_2026.parquet` | 09:53 EDT | 182 | **Yes** |
| `depth_charts` | `depth_charts_2026.parquet` | 09:53 EDT | 518,581 | **Yes** |

*Observed*, from the manifest as it stood on the date above. `players`,
`snap_counts`, and `schedules` are required — `store.load` raises if nothing is
on disk for them. `injuries` and `depth_charts` are `optional=True`: if the feed
goes dark, `store.load` logs a warning and returns an empty frame instead of
raising, and every field sourced from them comes back `null`.

### `data/out/dd-mm-yyyy-player-week-features.csv` / `player_week_features.parquet` — `features.FEATURE_COLUMNS` (25 fields)

Built by `features`, **no network** — pure DuckDB SQL over whatever parquet is
already mirrored to disk by the last `nflverse` run. Its freshness is entirely
inherited from that run; `features` itself never gets staler or fresher data on
its own.

| Field | Meaning | Sourced from (feed) | Our ingest |
|---|---|---|---|
| `gsis_id` | nflverse's internal player id, the join key throughout this layer | `snap_counts` (via `player_xwalk.csv`) | Rebuilt by `features` from disk; only as fresh as the last `nflverse` run |
| `season` / `week` | — | — | — |
| `player_name` / `position` / `team` | Latest-known values for that player (`arg_max(..., week)`) | `snap_counts` | — |
| `opponent` | That week's opponent | `snap_counts` | — |
| `espn_player_id` | Resolved via `player_xwalk.csv` | `players` (+ `db_playerids` fallback) | — |
| `played` | `True` if this player has a `snap_counts` row that week | `snap_counts` | — |
| `bye` | `True` if the player's team had no game that week — derived from the season's team-week expansion, **not** the absence of a snap row | `schedules` | — |
| `game_completed` | Whether that week's game for the player's team has finished | `schedules` | — |
| `offense_snaps` / `offense_pct` | Snap count / share of offensive plays | `snap_counts` | — |
| `targets` / `target_share` / `air_yards_share` / `wopr` | Usage metrics; `COALESCE`d to `0` when a `snap_counts` row exists but the matching `stats_player` row doesn't — a real zero, not missing data (verified live: 332/395 week-1 skill-position snap rows had a matching `stats_player` row; the other 63 — including a 64%-snap-share player — had none) | `stats_player` | — |
| `targets_per_snap` | `targets / offense_snaps`; `null` if `offense_snaps` is `null` or `0` | Derived | — |
| `snap_pct_delta_1w` / `snap_pct_delta_3w` / `snap_pct_trend` | Window functions computed **only over games the player actually played** — a bye or inactive week never interpolates as a silent 0 that would swing a stable starter's trend | Derived, from `snap_counts` | — |
| `report_status` / `practice_status` | Injury designation for that (player, week) | `injuries` (**optional**) | `null` if the feed is unavailable *or* the player has no injury designation that week — these two cases are indistinguishable in this column |
| `pos_rank` | Depth-chart rank at the player's position | `depth_charts` (**optional**) | **Not week-aligned.** `depth_charts` is an append-only log with no `week`/`season` column; `store.load` collapses it to the latest `dt` per team on read, so this is always "as of the last `nflverse` fetch," never "as of that week's depth chart" — joining it to a week-3 row and reading it as week-3 depth would be wrong |
| `provisional` | `True` if at least one game *leaguewide* that week hasn't finished yet — the single most cadence-relevant field in this table | `schedules` (derived) | Stat corrections land Tuesday/Wednesday; **Thursday's read is the first canonical one for the prior week** |

### `data/nflverse/player_xwalk.csv` — `ids.XWALK_COLUMNS` (8 fields)

Rebuilt every `nflverse` run from `players.parquet`, backfilled from
dynastyprocess where `espn_id` is null.

| Field | Meaning | Upstream release cadence | Our ingest |
|---|---|---|---|
| `gsis_id` | nflverse's internal id (never dropped, even with no ESPN match) | See `players` feed above | Rebuilt every `nflverse` run |
| `pfr_id` | Pro Football Reference id — the join key `snap_counts` actually carries | See `players` feed above | — |
| `espn_player_id` | Resolved ESPN id, numeric | See `players` feed above; backfilled from dynastyprocess (best-effort, unversioned) if null | — |
| `display_name` / `position` / `latest_team` / `status` | Player metadata as of the `players` feed | See `players` feed above | — |
| `source` | `players` (direct) or `db_playerids` (dynastyprocess fallback) | — | — |

### `data/nflverse/manifest.json` — the freshness record itself

Per-key (`{name}` or `{name}/{season}`) entries; this is what
`scripts/nflverse_coverage.py` reports on.

| Field | Meaning | Upstream release cadence | Our ingest |
|---|---|---|---|
| `last_updated` | The vendor's own `timestamp.json` value for that feed | — | Updated on every `nflverse` run, whether or not the asset itself changed |
| `etag` | HTTP ETag from the last successful download, used for the next conditional GET | — | — |
| `fetched_at` | Epoch seconds of our last *check* of this feed (not necessarily a download) | — | — |
| `rows` | Row count of the parquet on disk | — | `null` on a feed that has never downloaded successfully |
| `status` | `updated` / `unchanged` / `missing` / `error` from the last check | — | — |
| `message` | Error/skip detail, present on `missing`/`error` | — | — |

**Cadence caveats worth restating, all already established above but easy to
miss on a skim:**

- `is_stale(entry, hours=36)` is checked and printed by `nflverse` when a feed
  hasn't successfully updated in 36+ hours — but it's never enforced; the run
  continues regardless.
- This cadence runs in `.github/workflows/nflverse.yml` — 09/13/18 ET daily,
  plus a `--force` refresh Thursday morning for the canonical prior-week read.
  A fresh clone still runs nothing on its own clock; the workflows are the only
  scheduler. See `docs/automation.md`.

---

## The Odds API

**Access.** Base `https://api.the-odds-api.com/v4`, key in `ODDS_API_KEY`
(never logged -- `espn_ff/odds/client.py` redacts it from every URL,
exception, and error message at the single point a URL is stringified).
`regions=us` only, enforced at client construction (a comma anywhere in
`regions` throws before a request is built). `/v4/historical/*` is blocked
at the client layer, unconditionally -- it's paid-tier only and there is no
override. Retries 3 attempts, 2s exponential backoff, ≥1s spacing between
requests -- all three numbers deliberately smaller than the other three
clients' shared `4` / `1.5s`, because a retry here is a new billed request,
not a free do-over.

**Freshness mechanism.** *Documented*, and the one genuinely new mechanism
in this document: **freshness is purchased.** ESPN/Sleeper/nflverse are all
free to re-check, so their freshness ceiling is a TTL or an ETag. This feed
has a hard 500-credit-per-billing-period quota (`espn_ff/odds/ledger.py`)
and no historical endpoint, so nothing here refreshes on its own — every
value is exactly as fresh as the last job that had budget to run, and a
budget-aborted job leaves the prior snapshot in place with no visible
difference from a fresh one except `captured_at` and `last_run.json.stale`.
There is no `--refresh` flag anywhere in this layer, because re-running a
paid job is not free the way it is for the other three feeds.

### Market-open windows

The odds analogue of nflverse's feed-level cadence table — an exception to
the field-level rule below for the same reason nflverse's table is: these
three markets settle on genuinely different clocks, and every field in the
two output CSVs inherits its cadence from exactly one of them.

| Market | Opens | Settles | Our ingest |
|---|---|---|---|
| `spreads` / `totals` | Sun night for the coming week | Kickoff | Tue (`slate`), Fri (`line_movement`), Sun (`pre_lock`) — 3 pulls/wk, 2 credits each |
| player props | Wed–Thu | Kickoff | Thu (`props`), plus Sun (`pre_lock`) for undecided slots only |
| `scores` | Post-game | Final | Mon (`results`), `daysFrom=3` |

*Documented* (handoff §7). `espn_ff/odds/jobs.py:props_primary` refuses to
run before Wednesday for exactly this reason — an earlier pull is free but
returns nothing, which would otherwise look identical to "markets are
thin this week" rather than "markets aren't open yet."

### `odds-team-totals.csv` / `data/odds/team_totals.parquet` — `jobs._flatten_featured` + `projections.implied_team_totals`

Append-only, **never pruned** — see `espn_ff/odds/store.py`'s module
docstring for why this layer can't do what `sleeper/snapshots.py`'s
`KEEP_SLIM` does. Every `slate`/`line_movement`/`pre_lock` run adds rows;
none are ever deleted.

| Field | Meaning | Upstream release cadence | Our ingest |
|---|---|---|---|
| `captured_at` | ISO timestamp of the pull that produced this row — the actual freshness stamp, never the file's mtime | — | Observed |
| `week` | Scoring period the pull was taken for | — | — |
| `event_id` | The Odds API's own event id | — | — |
| `team` | ESPN abbreviation, resolved from the API's full team name via `names.TEAM_ALIASES` at flatten time (`spreads` rows only; `totals` rows are game-level and carry `team=None`) | — | — |
| `market` | `spreads` or `totals` | Sun night–Sun (see table above) | Documented |
| `book` / `outcome_name` / `price` / `point` | Raw per-book quote, kept alongside the derived `projections.consensus_line` median rather than only the median — so a book-level line-shopping question is still answerable later | Per-book, real time | Observed |
| `implied_team_total` | `projections.implied_team_totals`'s derived field: `total/2 - spread/2` per team, the DST and game-script signal | n/a — derived | Recomputed by `projections`, offline, from whatever's captured |

### `odds-player-props.csv` / `data/odds/player_props.parquet` — `jobs._flatten_event_odds` + `projections.prop_to_points`

Same append-only contract as team_totals.parquet.

| Field | Meaning | Upstream release cadence | Our ingest |
|---|---|---|---|
| `captured_at` / `week` / `event_id` | Same as team_totals.parquet | — | — |
| `player_name` | The Odds API's `description` field — a full name, and the **only** identifying information a prop carries; there is no player id anywhere in this feed | — | — |
| `team` | Filled in from `jobs.decision_events`'s roster walk where possible; `None` for a prop whose player isn't on either fantasy roster we happened to be tracking that week (e.g. surfaced only via a game-level market) | n/a — derived from our own roster, not the API | — |
| `market` / `book` / `outcome_name` / `price` / `point` | Raw per-book quote | Wed–Thu (see table above) | Documented |
| `espn_player_id` / `match_source` | Resolved by `espn_ff/odds/ids.py` — `xwalk` / `sleeper_map` / `espn_pool` / `unmatched`, name-first because the API gives no id and often no team either; **`unmatched` rows are kept, not dropped** | n/a — derived | Recomputed every `projections`/resolution run |
| `fantasy_points` | `projections.prop_to_points`'s derived field — the consensus line or de-vigged probability converted through `league_scoring.json`, never a hardcoded points-per-stat value | n/a — derived | Recomputed offline from whatever's captured |

### `data/odds/league_scoring.json`

Snapshot of `settings.scoring_frame`, written by the free `slate` job since
it already holds an authenticated `EspnClient`. Resolves handoff §12's open
item (PPR/half-PPR, TD values, DST tiers) by reading the league's actual
settings rather than hand-configuring them.

| Field | Meaning | Upstream release cadence | Our ingest |
|---|---|---|---|
| `stat_id` / `stat_abbrev` / `points` / `is_reverse` / `points_overrides` | Identical shape to `scoring-rules.csv` above (`settings.scoring_frame`) | League creation; effectively static in-season | Re-snapshotted every `slate` run, whether or not it changed |

### `data/odds/last_run.json`

Same shape and role as `data/sleeper/last_run.json`, but keyed per job name
(`slate` / `props` / `line_movement` / `pre_lock` / `results`) so one job's
staleness never masks another's.

| Field | Meaning | Upstream release cadence | Our ingest |
|---|---|---|---|
| `ran_at` | Epoch seconds of that job's last run, successful or not | — | Written every run of that job |
| `credits_spent` | Credits that specific run actually spent | — | — |
| `stale` | `true` if the run returned nothing (markets not open, budget aborted) — the CSVs on disk are the last good capture, not this run's | — | — |
| `reason` | Human-readable reason when `stale` is `true` | — | — |

### `data/odds/ledger.db` — the credit ledger itself

Unlike everywhere else in this document, the ledger is treated here as a
data artifact, not plumbing — its `spent_estimated` vs. `spent_authoritative`
split is exactly the kind of field-level caveat this document exists for.
Full invariants and the guard/reconcile contract live in
`docs/odds-budget.md`; this is the field reference.

| Table | Field | Meaning |
|---|---|---|
| `credit_ledger_entry` | `estimated_cost` | Worst-case pre-flight estimate (`markets_requested × regions`), recorded atomically with the budget check |
| | `actual_cost` | From `x-requests-last` once reconciled; equals `estimated_cost` on a `failed_assumed_charged` row |
| | `status` | `estimated` → `reconciled` (headers came back) or `failed_assumed_charged` (network error, or a response with no usage headers) |
| `credit_period_state` | `spent_estimated` | Running total of estimated cost for unreconciled entries plus actual cost for reconciled ones |
| | `spent_authoritative` | The high-water mark of the API's own `x-requests-used` header — catches usage from outside this app (a manual curl, another environment sharing the key) |
| | *(governing spend)* | `max(spent_estimated, spent_authoritative)` — always the more pessimistic of the two |
| `job_run` | `run_budget` / `spent_in_run` | Per-invocation budget, independent of the period budget — one runaway job can't eat the month even with quota to spare |

**Deferred, intentionally**: the §9 category-level weakness diagnostic
(roster vs. league-average production by rush/receiving/passing category).
It needs 4–6 weeks of accumulated snapshots that do not exist yet on a
fresh clone, and `/v4/historical/*` cannot backfill them. It ships as a
documented gap, not a noisy number computed on insufficient history —
render "insufficient history" rather than a number before then.

---

## Generated summaries

The one artifact here that is not ingested from a vendor at all — it is
produced from the reports this repo already renders, by `espn_ff summarize`.
It has no upstream cadence to report: it exists when a report exists and a
generation succeeded. `docs/ai-summaries.md` owns the prompt, the triggering
and the cost; this section covers only the stored fields.

### `summaries/<season>/week-NN/<YYYY-MM-DD>-<day>-<slug>.json` — `ai/summarize.envelope` (14 fields)

S3 only — gitignored locally, and written to the bucket's `summaries/`
prefix append-only.

| Field | Meaning | Upstream release cadence | Our ingest |
|---|---|---|---|
| `schema_version` | `1`. Bumped if the shape below changes incompatibly | — | — |
| `season` / `week` / `day` | Parsed from the report's path and filename; `day` is a `cli.REPORTS` key, so `tuesday-waivers` not `tuesday` | — | — |
| `report.path` | The **bucket key** of the report summarized, not the runner cache path it was read from | — | — |
| `report.source` | `s3` when the report came from the bucket mirror (the source of truth), `local` when the mirror was empty and the run fell back to the git checkout. A `local` value means report.yml's mirror step is not landing — the summary is still valid, the pipeline is not | — | — |
| `report.title` / `.covers` / `.week_window` / `.rendered` | Parsed verbatim off the 5-line block `render.header_lines` emits — read back from the artifact, never re-derived, so they describe the report on disk even if a later render would differ | — | — |
| `report.sha256` | Hex digest of the report text. **The freshness field**: it is what tells a later reader whether this summary still describes the report sitting beside it | — | — |
| `summary_markdown` | The generated prose. Plain markdown, no heading, no tables, capped at `prompt.WORD_CAP` words | — | — |
| `model` | The model id that produced it | — | — |
| `generated_at` | ISO-8601 **with an ET offset**, never naive UTC — same trap `render._fmt_ts` exists to avoid | — | Written every successful generation |
| `prior_reports` | Bucket keys of the up-to-4 same-season, same-type reports supplied as context, oldest-first. Empty for the first report of a season | — | — |
| `prompt_sha256` | Hex digest of the system instruction plus user message. Tells a reader whether the prompt that produced this summary is still the one in the tree | — | — |
| `usage` | `promptTokenCount` / `candidatesTokenCount` / `totalTokenCount` as the vendor reported them; zeros when the response carried no `usageMetadata` | — | — |

**There is no staleness flag here, deliberately.** Every other artifact in
this document carries one because a feed can go dark and leave old data
looking current. A summary cannot: `report.sha256` and `prompt_sha256` make
staleness *computable* by the reader rather than asserted by the writer, and
a summary whose report hash no longer matches is not stale, it is describing
a different document. That is a stronger statement than a boolean, and it is
the same "freshness from the artifact, never from mtime" rule this document
applies everywhere else, pushed one level further.

---

## Closing notes

**Deferred, intentionally** (worth listing so this reads as a complete audit,
not an incomplete one): play-by-play red-zone/inside-the-5 metrics, the
`ffopportunity` expected-points feed, a FantasyPros ECR baseline, and the
`recommendations` backtest loop. None of these are ingested anywhere in this
codebase today.

**Known freshness gaps:**

- Every artifact above is exactly as fresh as the last time the relevant
  command ran. That is now usually a GitHub Actions run dispatched on a
  schedule rather than a person (`docs/automation.md`), which changes who
  triggers a pull but not this document's answer for any field. A slot can
  still fail, but it can no longer fail quietly: the schedules moved to AWS
  EventBridge precisely because GitHub's own cron dropped slots with no signal,
  and a dispatch that does not land now raises an alarm
  (`docs/aws-scheduling.md`).
- **No cadence here has been re-measured against the automated schedule.** The
  Observed figures below were taken from manual runs; nothing has yet run a full
  NFL week unattended.
- Every ESPN fetch site now applies `cache.ttl_for` — the weekly-roster fetch
  via `ttl_for(week, current)`, and `teams.csv`, `matchups.csv`, `draft.csv`,
  `transactions.csv`, and `player-pool.csv` via `ttl_for(None, current)` — so
  none of them are served forever any more; `--refresh` remains for forcing an
  immediate re-fetch sooner than `LIVE_TTL` (300s) would on its own. This
  closes a real incident: a local `export` cached a pre-week-1 standings
  snapshot and a pre-Monday-Night matchup schedule with no expiry, and every
  local render since built on that stale snapshot until the underlying fetch
  sites were fixed. (`current_scoring_period()` had this same cache-forever
  bug on its own underlying fetch even earlier; it's resolved from the
  on-disk season calendar instead — see "Freshness mechanism" above.)
- Historical `depth_charts` snapshots are not retained — `store.load` always
  collapses to the latest `dt`, so there is currently no way to ask "what was
  the depth chart in week 3" after week 4 has landed.
- ESPN session cookies expire silently and surface only as a `401` on the next
  `probe` (or any other command) — there is no advance warning.
- **No backfill is possible for the Odds API layer.** `/v4/historical/*` is
  paid-tier and blocked at the client, so this layer's history begins the day
  the first job runs, with no pre-history, ever — unlike ESPN/Sleeper/nflverse,
  which can all be re-pulled for a past date.
- A budget-aborted odds job leaves the prior snapshot in place, and a stale
  line looks identical to a fresh one on disk — read `captured_at` and
  `last_run.json.stale`, never the parquet file's mtime.
- An empty odds props response is free and is **not** success; it means the
  market hasn't opened yet, not that no props exist this week.
- The odds name join (`espn_ff/odds/ids.py`) is the only name join in this
  repo where the API gives no id and often no team hint either;
  `match_source == "unmatched"` rows are present in `player_props.parquet` and
  unscored, by design, rather than silently dropped.

**Before trusting any table above, check actual freshness, not just this
document:** `scripts/practice_coverage.py` reports how complete
`practice_participation` really is before you lean on the `tier` column for a
start/sit call, and `scripts/nflverse_coverage.py` reports id-resolution and
manifest-freshness for the nflverse layer.
