# ESPN Fantasy Football Sandbox

Pull data from ESPN's fantasy football API into tidy CSVs.

There is no official public ESPN fantasy API — this targets the undocumented
internal API the fantasy site itself calls. No API key, and it can change
without notice (ESPN moved the base host in April 2024 with no announcement).

Default target: league `1681721675`, season `2026`, team `5`.

## Setup

```bash
uv venv && uv pip install requests pandas pytest
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
```

Flags: `--season`, `--league-id`, `--team-id`, `--week`, `--weeks 1-18`,
`--refresh` (bypass cache), `--depth-lookback` (default 3, for `status`),
`--trending-limit` (default 25), `--trending-floor` (default 0).

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
| `scripts/probe.py` | Dump key paths from a cached payload |
| `scripts/practice_coverage.py` | practice_participation coverage report |

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

## Tests

```bash
.venv/bin/pytest
```

Run entirely off fixtures in `tests/fixtures/` — no network. The fixture holds
five real players chosen so that some list 2026 first in `stats[]` and others
list 2025 first, which is what makes the ordering bug reproducible.

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
