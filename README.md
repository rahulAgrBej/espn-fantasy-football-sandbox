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

`summarize` needs a third key, `GEMINI_API_KEY`, from
[aistudio.google.com](https://aistudio.google.com). Its token spend is not
metered against a fixed quota — a summary costs roughly two cents — but its
**Google Search grounding is**: 5,000 free search queries a month, then $14
per 1,000, billed per query the model chooses to run rather than per
request. Measured use is ~10 searches a report, ~340 a month, about 7% of
the free allowance — so there is still no ledger and no guard, but there is
now a quota to watch; every envelope records the searches it spent. See
`docs/ai-summaries.md`. The key travels as an
`x-goog-api-key` request header and never as a URL query parameter, so there
is no URL or exception for it to leak through, and the same pre-commit scan
applies.

### Keeping secrets out of git

Two layers. `.gitignore` covers `.env*`, `*.env`, `.envrc`, `.Renviron`,
`CLAUDE.md` and friends, `.claude/`, `data/`, `summaries/`, `.cache/`, and
key/pem files.

Because `.gitignore` does nothing against `git add -f` or an already-tracked
file, `.githooks/pre-commit` rejects any commit that stages one of those paths.
Enable it once per clone:

```bash
git config core.hooksPath .githooks
```

It is already enabled here. `git commit --no-verify` still bypasses it -- a
hook is a guardrail, not a sandbox. It is also per-clone: a fresh clone has
no hook until that command is re-run.

A third layer covers what the first two structurally cannot. GitHub secret
scanning with push protection rejects a matching push outright, which is
the only one of the three that survives `--no-verify` or a clone that never
configured the hook. CI itself holds no AWS credentials at all -- workflows
assume a role via OIDC, scoped to this repo and the `gh_env` environment,
with permissions that stop at a single S3 bucket. See `docs/automation.md`.

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
.venv/bin/python -m espn_ff report --day monday   # render one weekly report -> reports/ (no network)
.venv/bin/python -m espn_ff summarize        # AI summary + grounded roster news for every report owing either -> summaries/
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
event ids to restrict the pull to), `--day` (which report to render, or a
filter for `summarize`), `--limit` / `--reports-dir` / `--summaries-dir` /
`--summaries-out` / `--no-news` (`summarize`; `--force` there regenerates
both layers of an envelope that already has them, and `--no-news` skips the
metered grounded calls and leaves the news to be backfilled later).

`summarize` produces two things per report, from prompts with opposite
rules: a prose summary that may use *only* what the report says, and a
Google-Search-grounded news block that may use *only* what it just searched.
They share one envelope and fail independently -- a dead grounded call
stores `news: null`, keeps the summary, and the next run fills in just the
news.

`summarize` never exits non-zero -- a missing key, a dead model or a dropped
connection all print to stderr and return 0, because a summary is additive
and the report it describes is already written. See `docs/ai-summaries.md`.

CSVs land in `data/out/` as `dd-mm-yyyy-name.csv`, and are mirrored to S3
by the scheduled workflows -- see `docs/automation.md`.

Prefer `gh workflow run` over running these commands locally -- see
`CLAUDE.md`'s "Running anything that touches `data/`".

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
| `espn_ff/report/` | The eight weekly markdown reports and their shared loaders/renderers |
| `espn_ff/ai/` | Generated report summaries and grounded roster news — Gemini client, two opposed prompt modules, discovery, JSON envelope |
| `scripts/probe.py` | Dump key paths from a cached payload |
| `scripts/practice_coverage.py` | practice_participation coverage report |
| `scripts/nflverse_coverage.py` | nflverse id-resolution and manifest-freshness coverage report |
| `docs/data-sources.md` | Field-level freshness reference for every output CSV, across all four feeds |
| `docs/odds-budget.md` | The Odds API's credit-budget invariant, cost table, job schedule, and guard runbook |
| `docs/automation.md` | How the schedule runs unattended: workflows, S3 state/archive split, OIDC, runbook |
| `docs/aws-scheduling.md` | Why the clock lives in AWS, the EventBridge schedules, and how a failed trigger alarms |
| `docs/ai-summaries.md` | The generated summaries and grounded roster news: the two prompts, triggering, the v2 envelope, both cost meters, and why the command always exits 0 |
| `.github/workflows/` | The five collection workflows, the report renderer and its summarizer, plus CI — all dispatched from AWS |
| `scripts/s3_sync.sh` | Restore/archive/push the `data/` tree against S3 |
| `scripts/rotate_espn_cookies.sh` | Rotate the ESPN cookies and validate them in CI |
| `scripts/rotate_dispatch_token.sh` | Rotate the PAT AWS dispatches with, and prove it end to end |
| `infra/*.json.example` | IAM trust + S3 policy templates (`<ACCOUNT_ID>` placeholders) |
| `infra/scheduler.yaml` | CloudFormation stack for the EventBridge schedules, rules, DLQ and alarm |

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
week's games aren't all complete yet.

This cadence runs in GitHub Actions — `.github/workflows/nflverse.yml`
pulls at 09/13/18 ET daily and forces a full refresh Thursday morning. See
`docs/automation.md` for the workflow set, the S3 layout, and the runbook.
A local clone still runs nothing on its own clock; the workflows are the
only scheduler, and `workflow_dispatch` fires any of them by hand.

nflverse data is built by [nflverse](https://github.com/nflverse) from
public NFL data plus [Pro Football Reference](https://www.pro-football-reference.com/)
snap counts; the id crosswalk falls back to
[dynastyprocess](https://github.com/dynastyprocess/data)'s `db_playerids.csv`.
That data is not redistributed from this repository — `data/` is
gitignored and nothing fetched from nflverse or PFR is committed. The
collected artifacts live in a private S3 bucket, not in git.

The Odds API layer runs on its own workflow, `.github/workflows/odds.yml`,
kept separate for the reason stated throughout `docs/odds-budget.md`: **the
nflverse commands above are idempotent and free to re-run; these are not.**
Re-running `odds props` spends credits again, even if nothing about the
market changed. Its five cron slots map one-to-one onto that document's job
schedule, and three guards sit in front of them — a single `odds-ledger`
concurrency group so two runs can never race the credit ledger, an
off-season check, and a `--dry-run` preflight that prices the job using
only the free `/events` endpoint before anything is issued.

A manual run is still the right tool for anything ad hoc:

```bash
# Price it first -- issues nothing.
.venv/bin/python -m espn_ff odds props --dry-run
gh workflow run odds.yml -f job=props -f dry_run=false   # or run it in CI
```

## Weekly collection schedule

Times are ET and stay ET — the schedules are pinned to `America/New_York`, so
no slot shifts when DST ends. *(Observed — EventBridge expressions in
`infra/scheduler.yaml`.)* The compute runs on GitHub Actions but the clock is
AWS EventBridge Scheduler; see `docs/aws-scheduling.md` for why and how.
`docs/data-collection-weekly-schedule.md` explains what each row means for a
lineup or waiver decision, and `docs/automation.md` has the workflow/S3 runbook.

| Day | Time (ET) | Data metric | Data source |
|---|---|---|---|
| Monday | 08:11 daily | Daily player status refresh | Sleeper — `sleeper` |
| Monday | 09:08 | Final scores, closed matchup results, weekend transactions | ESPN — `--refresh` on `matchups.csv` / `transactions.csv` |
| Monday | 09:38 | Prior week's game results (`daysFrom=3`) | The Odds API — `results` job |
| Tuesday | 09:08 | Pending waiver claims ahead of tonight's processing run | ESPN — `--refresh` on `transactions.csv` |
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

The eight reports render at Mon 10:30, Tue 10:00, Tue 11:00, Wed 10:00,
Thu 11:00, Fri 11:00, Sat 10:00 and Sun 11:30 ET
(`docs/report-weekly-schedule.md`).
Each one's AI summary and grounded roster news follow within seconds, off a
`workflow_run` event rather than a clock, with an EventBridge backstop 20
minutes behind each slot — `docs/ai-summaries.md`. The news is the only
thing in this pipeline sourced from the open web rather than from a feed,
which is why it is stored beside the summary and never merged into it.

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
