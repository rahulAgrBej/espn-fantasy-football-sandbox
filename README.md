# ESPN Fantasy Football Sandbox

Pull data from ESPN's fantasy football API into tidy CSVs.

There is no official public ESPN fantasy API — this targets the undocumented
internal API the fantasy site itself calls. No API key, and it can change
without notice (ESPN moved the base host in April 2024 with no announcement).

Default target: league `1681721675`, season `2026`, team `5`.

## Setup

```bash
uv venv && uv pip install requests pandas duckdb pyarrow pytest
```

### Credentials

This league is **private** — unauthenticated requests return
`401 AUTH_LEAGUE_NOT_VISIBLE`. You need two session cookies from a logged-in
browser: DevTools → Application → Cookies → `fantasy.espn.com`.

Create a `.env` in the project root (it is gitignored, and the pre-commit hook
below refuses to commit it):

```
ESPN_S2=<the espn_s2 cookie value, url-encoded exactly as the browser shows it>
SWID={XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}
```

`SWID` includes the surrounding braces. Cookies are read via `os.environ` and
never appear in code. Shell exports work too and take precedence over `.env`.
These are session credentials -- they expire, and `probe` will start returning
401 when they do.

The public player pool and the stat dictionary need no credentials.

The Odds API layer needs its own key, `ODDS_API_KEY`, from
[the-odds-api.com](https://the-odds-api.com). Unlike the ESPN cookies above,
this credential is **metered** -- 500 free-tier credits per billing period,
no historical endpoint -- so leaking it or over-using it has a real dollar
cost, not just an inconvenience. It is never logged (every URL, exception,
and error message is redacted before it's printed) and the pre-commit hook
below blocks any staged file containing an `ODDS_API_KEY=<value>` assignment
or a bare 32-hex-character key.

### Keeping secrets out of git

Two layers. `.gitignore` covers `.env*`, `*.env`, `.envrc`, `.Renviron`,
`CLAUDE.md` and friends, `.claude/`, `data/`, and key/pem files.

Because `.gitignore` does nothing against `git add -f` or an already-tracked
file, `.githooks/pre-commit` rejects any commit that stages one of those paths.
Enable it once per clone:

```bash
git config core.hooksPath .githooks
```

It is already enabled here. `git commit --no-verify` still bypasses it -- a
hook is a guardrail, not a sandbox.

## Commands

```bash
.venv/bin/python -m espn_ff probe            # confirm auth, list teams
.venv/bin/python -m espn_ff team --week 1    # one team's roster and points
.venv/bin/python -m espn_ff pull             # cache raw JSON for all views
.venv/bin/python -m espn_ff export           # cached JSON -> tidy CSVs
.venv/bin/python -m espn_ff sleeper          # daily Sleeper fetch/slim/resolve (batch job)
.venv/bin/python -m espn_ff status           # derived player-status table -> data/out/
.venv/bin/python -m espn_ff nflverse         # daily nflverse fetch + crosswalk (batch job)
.venv/bin/python -m espn_ff features         # derived player-week role-feature table -> data/out/
.venv/bin/python -m espn_ff odds <job>       # metered! one of: slate, props, line_movement, pre_lock, results
.venv/bin/python -m espn_ff projections      # derived betting-market projections -> data/out/ (no network)
.venv/bin/python -m espn_ff credits          # credits used / 500 (free, local, reads the ledger only)
```

Flags: `--season`, `--league-id`, `--team-id`, `--week`, `--weeks 1-18`,
`--refresh` (bypass cache), `--refresh-weeks` (force a re-pull of just these
scoring periods -- same spec as `--weeks` -- bypassing the cache for
week-scoped ESPN fetches only; plain `--refresh` wins if both are given),
`--depth-lookback` (default 3, for `status`),
`--trending-limit` (default 25), `--trending-floor` (default 0),
`--seasons` (range like `2024-2026` or list like `2024,2026`, for
`nflverse`/`features`; defaults to the current season), `--force` (bypass
the nflverse timestamp short-circuit for `nflverse`; bypass only the
Wednesday weekday check -- never the credit budget -- for `odds props`),
`--min-snap-pct` (display filter on the `features` CSV, default 0),
`--dry-run` (odds: estimate the credit cost and check the guard without
issuing anything), `--events` (odds `props`/`pre_lock`: comma-separated
event ids to restrict the pull to).

CSVs land in `data/out/` as `dd-mm-yyyy-name.csv`.

## Layout

| Path | Role |
|---|---|
| `espn_ff/client.py` | ESPN HTTP client |
| `espn_ff/cache.py` | Raw JSON cache keyed by request |
| `espn_ff/stats.py` | Stat-row selection — see below |
| `espn_ff/constants.py` | Slot/position maps; stat dictionary from ESPN |
| `espn_ff/extract/` | One module per ESPN view → tidy DataFrame |
| `espn_ff/sleeper/` | Sleeper player-status layer — client, snapshots, ESPN id join, derived signals |
| `espn_ff/nflverse/` | nflverse role-feature layer — client, parquet store, ESPN id crosswalk, derived features |
| `espn_ff/odds/` | The Odds API betting-market layer — credit ledger, metered client, parquet store, name join, derived projections |
| `scripts/probe.py` | Dump key paths from a cached payload |
| `scripts/practice_coverage.py` | practice_participation coverage report |
| `scripts/nflverse_coverage.py` | nflverse id-resolution and manifest-freshness coverage report |
| `docs/data-sources.md` | Field-level freshness reference for every output CSV, across all four feeds |
| `docs/odds-budget.md` | The Odds API's credit-budget invariant, cost table, job schedule, and guard runbook |

## Sleeper player-status layer

ESPN collapses every "Questionable" into one undifferentiated `injuryStatus`
string. Sleeper's free, unauthenticated read API adds practice participation,
depth chart order, injury detail and community add/drop trending, joined onto
ESPN's `player_id` in `espn_ff/sleeper/ids.py`.

`sleeper` is the daily batch job: fetch + slim the player pool (once a day,
per Sleeper's own ask), pull trending, resolve ids against ESPN, and fail
loudly if anyone on your own roster comes back unmatched. `status` builds the
derived table (availability tier, Wed/Thu/Fri practice trajectory, depth
chart delta, trending flag) from the snapshots already on disk — no network.

Practice trajectory needs three consecutive daily `sleeper` runs to fill in;
before that it reads `— / — / —` or partial. `scripts/practice_coverage.py`
reports how complete `practice_participation` actually is before trusting the
tier table for a start/sit call.

Trending is alerting context only — it is shown, never scored or sorted on.

## The nflverse role layer

ESPN and Sleeper both describe *availability* — who can play. Neither
publishes anything like snap share, which is the strongest available signal
for *role* — how much of the offence a player actually accounts for.
[nflverse](https://github.com/nflverse/nflverse-data) publishes `offense_pct`
per player-game and `target_share` / `air_yards_share` / `wopr` per
player-week as free public GitHub release assets, rebuilt several times a
day. `nflverse` fetches and mirrors those assets to `data/raw/nflverse/`;
`features` derives `player_week_features.parquet` from them, offline, with
DuckDB querying the parquet in place.

**The id chain, and why it is never a name join.** nflverse's own id is
`gsis_id`, not ESPN's `player_id`. The crosswalk in `espn_ff/nflverse/ids.py`
resolves `gsis_id <-> espn_id` directly off `players.parquet` (both ids ship
on the same row), falling back to dynastyprocess's `db_playerids.csv` only
where that's null. Snap-level data carries a third id, `pfr_player_id`
(Pro Football Reference), which the crosswalk also carries as `pfr_id` so
`snap_counts` can join to `gsis_id` without ever touching a player's name.
Verified against the live 2026 pool: 168 of 181 week-1 roster rows matched
`espn_id` directly, and all 13 misses were D/ST — nflverse has no player row
for a team defence by design, so those are excluded before the join, not
counted as orphans. Real orphans that week: zero (a since-elevated
practice-squad player the following week is the expected steady state).

**The missing-stats-row trap.** A `snap_counts` row for a player who did not
also appear in `stats_player` means *zero production, not missing data* — he
played, and produced nothing. Verified live: only 332 of 395 week-1
skill-position snap rows had a matching `stats_player` row; Calvin Ridley
played 32 snaps (64% of the offense) in week 1 with no `stats_player` row at
all. `espn_ff/nflverse/features.py` `COALESCE`s `targets`,
`target_share`, `air_yards_share` and `wopr` to 0 on that join — dropping
that `COALESCE` would silently erase every low-usage starter's week from the
table, exactly the players a role signal exists to catch.

**Bye vs. inactive vs. not-yet-signed.** These three "no snap data this
week" cases must never collapse into one:

| Case | `bye` | `played` | `offense_pct` |
|---|---|---|---|
| Team had no game (bye) | `true` | `false` | `null` |
| Team played, player did not appear | `false` | `false` | `null` |
| Not yet on the team / no longer on it | *(row absent entirely)* | — | — |

A bye is derived from `games.parquet`: a `(team, week)` pair absent from
the season's team-week expansion. `snap_pct_delta_1w`/`_3w` and
`snap_pct_trend` are computed over games the player actually played, never a
dense week axis — a stable starter's trend must read flat across his own
bye, not swing to "falling" then "rising" around a week of no information.

**`depth_charts` has no `week` column** — it is an append-only log keyed
only on a `dt` timestamp, 178 distinct snapshots and 518k+ rows for the 2026
season alone. `store.load("depth_charts", ...)` collapses it to the latest
`dt` per team on read; anyone who wants "the depth chart as of week 3" needs
a snapshot taken in week 3, which this layer does not yet retain (see
Deferred, below).

**Deferred**, intentionally, out of this layer: play-by-play red-zone/inside-
the-5 metrics, the `ffopportunity` expected-points feed, a FantasyPros ECR
baseline, and a `recommendations` backtesting loop.

## The betting-market layer

ESPN, Sleeper and nflverse all describe *what already happened* or *who can
play*. None of them carries a forward-looking, market-priced projection,
which is what the start/sit question actually needs: where is my lineup
weak, who do I start, what are my odds this week. A sportsbook's line is
exactly that kind of projection — priced by people with money on the
outcome — and `espn_ff/odds/` layers it onto the ESPN roster.

**Implied team totals are the DST and game-script signal.** `spreads` and
`totals` from one `/odds` call cover the entire slate for 2 credits;
`projections.implied_team_totals` converts them per team via
`total/2 - spread/2` — a favorite's negative spread number *adds* to half
the total, an underdog's positive number subtracts from it.

**The credit budget is an enforced invariant, not a guideline.** 500
credits per billing period, free tier, no historical endpoint. Every
request passes through `espn_ff/odds/ledger.py:guard` — one atomic
transaction that reads spend, checks it against budget, and records the
new request, so two concurrent jobs can never both pass on stale state.
There is no bypass flag anywhere in this package; see `docs/odds-budget.md`
for the full contract.

**The de-vig step, and why a raw `player_anytime_td` price overstates.**
A two-sided market's raw prices always sum to more than 100% implied
probability — that's the book's vig. `projections.devig_two_way` strips it
by normalizing both sides so they sum to exactly 1, and that de-vigged
probability is what gets multiplied by the league's own points-per-TD
value (from `league_scoring.json`, never a hardcoded 6) to become fantasy
points.

**The name-join caveat.** The Odds API ships no player ids at all — a
prop's `description` field is a full name, nothing else, and a game odds
response doesn't even say which team a player is on. `espn_ff/odds/ids.py`
matches on name first, using a team hint (filled in from our own roster
walk, where available) only to break a tie on a shared surname. Unmatched
rows are kept, not dropped — a prop on a just-signed player is exactly the
signal this layer exists to surface.

**History only exists if we save it.** `/v4/historical/*` is paid-tier and
blocked at the client, unconditionally, so `player_props.parquet` and
`team_totals.parquet` are append-only and never pruned — see
`espn_ff/odds/store.py`'s module docstring. Every other feed in this repo
can be re-fetched for free if a snapshot gets lost; this one can't.

**Deferred, intentionally**: the roster-vs-league-average weakness
diagnostic by category (rush/receiving/passing). It needs 4–6 weeks of
accumulated snapshots that don't exist on a fresh clone and cannot be
backfilled — it ships as a documented gap, not a noisy number.

## The stats[] trap

A player's `stats[]` array mixes seasons, sources and split types in one flat
list. Filtering on `statSourceId` and `scoringPeriodId` alone — what most
community snippets do — returns whichever row happens to sit first in the
array. Across a 300-player sample of the live 2026 pool, **128 of the 280
players with week-1 data resolved to the 2025 row**.

Every selector in `espn_ff/stats.py` keys on all four fields:

| Field | Meaning |
|---|---|
| `seasonId` | the season — the one most code forgets |
| `scoringPeriodId` | week number; `0` on season aggregates |
| `statSourceId` | `0` actual, `1` projected |
| `statSplitTypeId` | `0` season, `1` game, `2` rest-of-season |

The source and split values are not guesses — they come from ESPN's own
`sources` / `splitTypes` lookups on the public platform-settings endpoint.

## The live-scoring trap

While a scoring period is open, a matchup's `totalPoints` is `0.0` and the real
number sits in `totalPointsLive`. Reading `totalPoints` alone scores every
in-progress matchup as a 0-0 tie. `matchups_frame` prefers the live value and
takes W/L/T from ESPN's own `winner` field, which reads `UNDECIDED` until the
week closes, rather than inferring a result by comparing two placeholder zeros.

Two other normalisations in the transaction log: team id `0` means free agency,
not team 0, and lineup slot `-1` means "no slot". Both become `None`.

## Caching

Fantasy data is immutable once a scoring period closes, so completed weeks are
cached forever and only the live week is re-fetched (5 min TTL). This matters:
`mRoster` returns the roster *as of* a given `scoringPeriodId`, so a full season
of weekly rosters is one request per week. Rate limits are undocumented —
hammering the API during live games is how people get blocked.

The current week itself is resolved the same cache-friendly way: ESPN publishes
the whole season's week-by-week calendar (`scoringPeriods`, with a start/end
date per week) in one payload, so "what week is it" is answered by comparing
`now` against that already-cached calendar rather than trusting a single
frozen field. It only re-fetches that calendar when the cached copy can no
longer answer the question — missing, `now` outside every window, or a week
boundary crossed since the last fetch — and even then at most once every 5
minutes. To force one specific week's ESPN data to re-pull without bypassing
the whole cache, use `--refresh-weeks` (e.g. `--refresh-weeks 3`).

## Refresh cadence

nflverse feeds settle over the course of the day as source data lands; both
`nflverse` and `features` are idempotent and cheap to re-run — a re-run
against an unchanged `timestamp.json` makes no parquet download at all.
Thursday's read is the canonical one for the prior week: stat corrections
land Tuesday/Wednesday, so anything read before Thursday should be treated
as `provisional = true`, which is exactly the flag `features` sets when a
week's games aren't all complete yet. Nothing here is wired into cron —
that's a deliberate choice so a fresh clone never surprises anyone with a
background job. A commented starting point:

```cron
# nflverse settles through the day; both commands are idempotent.
0 9,13,18 * * *  cd /path/to/repo && .venv/bin/python -m espn_ff nflverse && .venv/bin/python -m espn_ff features
# Thursday's read is canonical for the prior week -- stat corrections land Tue/Wed.
0 9 * * 4        cd /path/to/repo && .venv/bin/python -m espn_ff nflverse --force && .venv/bin/python -m espn_ff features
```

nflverse data is built by [nflverse](https://github.com/nflverse) from
public NFL data plus [Pro Football Reference](https://www.pro-football-reference.com/)
snap counts; the id crosswalk falls back to
[dynastyprocess](https://github.com/dynastyprocess/data)'s `db_playerids.csv`.
This project stays private for now, per that data's terms.

The Odds API layer is not on this cadence, and deliberately not on this
crontab, for the reason stated throughout `docs/odds-budget.md`: **the
nflverse commands above are idempotent and free to re-run; these are not.**
Re-running `odds props` spends credits again, even if nothing about the
market changed. A commented starting point, opted into explicitly and only
after `ODDS_API_KEY` is set:

```cron
# Metered -- unlike the nflverse crontab above, none of these are free to
# re-run. Each line matches one row of docs/odds-budget.md's job schedule.
0 9  * * 2  cd /path/to/repo && .venv/bin/python -m espn_ff odds slate
0 10 * * 4  cd /path/to/repo && .venv/bin/python -m espn_ff odds props
0 10 * * 5  cd /path/to/repo && .venv/bin/python -m espn_ff odds line_movement
30 10 * * 0 cd /path/to/repo && .venv/bin/python -m espn_ff odds pre_lock
0 9  * * 1  cd /path/to/repo && .venv/bin/python -m espn_ff odds results
```

## Tests

```bash
.venv/bin/pytest
```

Run entirely off fixtures in `tests/fixtures/` — no network. The ESPN fixture
holds five real players chosen so that some list 2026 first in `stats[]` and
others list 2025 first, which is what makes the ordering bug reproducible.
The nflverse fixtures under `tests/fixtures/nflverse/` are small hand-built
CSVs (read via pandas, not committed parquet, so they stay diffable) across
a 5-team, 3-week universe engineered to exercise a bye sandwiched between two
played weeks, a snap row with no `stats_player` row, a zero-snap week, and an
unresolvable `pfr_player_id` orphan. The odds fixtures under
`tests/fixtures/odds/` are hand-trimmed `events`/`featured_odds`/`event_odds`/
`scores` JSON, same "small and diffable" spirit as the nflverse CSVs, and
**none of them contains a key** -- every odds test uses a fake session that
records calls rather than a real `ODDS_API_KEY`, so the suite spends zero
credits. A separate `odds_live` pytest marker exists for a future test that
actually spends real credits; nothing under it runs by default.

## Endpoint reference

Base, 2018 and later:

```
https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leagues/{leagueId}
```

Seasons 2017 and earlier use `.../ffl/leagueHistory/{leagueId}?seasonId={season}`.
Swap `ffl` for `fba`, `flb`, `fhl` for other sports.

Views: `mSettings`, `mTeam`, `mRoster`, `mMatchup`/`mMatchupScore`, `mBoxscore`,
`mLiveScoring`, `mStandings`, `mDraftDetail`, `mTransactions2`,
`mPendingTransactions`, `mPositionalRatings`, `kona_player_info`, `players_wl`,
`allon`. Passing several in one request returns a richer joined payload than
fetching them separately.

Player endpoints cap at 50 results and ignore query-string sorting; use the
`X-Fantasy-Filter` JSON header instead (`client.get_player_pool` does).
