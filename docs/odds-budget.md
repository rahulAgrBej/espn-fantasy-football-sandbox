# The Odds API credit budget

The operational contract for `espn_ff/odds/`, separate from
`docs/data-sources.md`'s field-level freshness reference because this is
about the invariant that makes the whole layer safe to run at all, not
about any one field's cadence.

## The 500-credit invariant

500 credits per billing period, free tier, no historical endpoint. Every
code path that can issue a metered request passes through
`espn_ff/odds/ledger.py:guard`, which does the read, the budget check, and
the ledger append as one atomic SQLite transaction — no two concurrent
jobs can both pass on stale state, and there is no override parameter
anywhere in this package. A ledger read failure blocks the request; it is
never caught into a default that lets the request through.

`guard` is checked against `spent = max(spent_estimated, spent_authoritative)`
— always the more pessimistic of an optimistic running estimate and the
API's own `x-requests-used` header, so usage from outside this app (a
manual curl, another environment sharing the key) is never invisible to
the guard. `RESERVE = 40` credits are held back at `priority="normal"`;
only the Sunday `pre_lock` job may pass `priority="critical"` to draw that
reserve to zero, and `guard` asserts on the job name so nothing else can.

Every scheduled job also declares its own **run budget**
(`job_run.run_budget`), enforced independently of the period budget — a
single runaway job can't eat the month even with quota to spare.

## Cost table

| Endpoint | Cost | Notes |
|---|---|---|
| `GET /v4/sports` | 0 | Free — the startup check that `americanfootball_nfl` is still live. |
| `GET /v4/sports/{sport}/events` | 0 | Free — event ids, teams, kickoff times. |
| `GET /v4/sports/{sport}/odds` | `markets × regions` | All games on the slate; `regions` is always exactly `us` (1), so cost = market count. |
| `GET /v4/sports/{sport}/events/{id}/odds` | `unique_markets_returned × regions` | One game. Billed on markets *returned*, not requested — the estimate uses markets requested (worst case), reconciled to `x-requests-last` after. |
| `GET /v4/sports/{sport}/scores` | 1, or 2 with `daysFrom` | `results` always uses `daysFrom=3`, so cost 2. |
| `GET /v4/historical/*` | 10× | **Blocked at the client**, unconditionally. There is no code path that can reach this. |

Empty responses are free but not free of consequence — an empty props
response almost always means the market hasn't opened yet, not that
nothing changed; `last_run.json` records `stale=true` with a reason
either way.

**That "market hasn't opened yet" reason hid a real bug for the whole life
of this layer.** `/events` returns every upcoming NFL event — two or more
weeks of them at once — and `jobs._events_by_team` built its team → event
index by assigning as it walked the list, so the *last* event naming a team
won: the furthest-out one. Every `props` run therefore bought markets on
games 8–12 days away, got zero bookmakers back, and wrote nothing, while
`last_run.json` reported the entirely plausible "no props returned; markets
likely not open yet". **Observed 2026-09-17**: 10 events fetched, every
cached payload under `data/raw/odds/**/event_odds/` carrying a
`commence_time` of 2026-09-25..29 while week 2's games ran 09-17..09-21,
and `player_props.parquet` never written once. The index now takes each
team's *earliest* upcoming event and drops anything outside the fantasy
week (`weeks.week_window`), so a bye-week team maps to nothing rather than
to next week's game.

The credit consequence is the reason this went unnoticed rather than
showing up as overspend: those calls cost nothing, because
`/events/{id}/odds` bills on **markets returned**, not requested, and an
unopened game returns none. A silent zero-cost failure is the worst shape a
metered job can fail in — it looks identical to a quiet week.

## The five-job schedule

| Job (`odds <job>`) | Slot (ET) | Run budget | Priority | Typical cost |
|---|---|---|---|---|
| `slate` | Tue 09:38 | 4 | normal | 2 (spreads+totals, all games) |
| `props` | Thu 10:08 | 40 | normal | ~10–20 (6–10 decision-relevant events, 3–4 markets each) |
| `line_movement` | Fri 10:08 | 4 | normal | 2 |
| `pre_lock` | Sun 10:38 | 25 | **critical** | ~5–15 (featured + undecided-slot props only) |
| `results` | Mon 09:38 | 4 | normal | 2 (`daysFrom=3`) |

Steady state: roughly 50–67 credits/week, ~250–335 per billing period out
of 500 — the remainder is headroom for playoff weeks and ad-hoc research.

Each slot is dispatched by its own EventBridge schedule, which names the job
explicitly and sends `dry_run=false` (`docs/aws-scheduling.md`). Those five are
the only schedules in the repo that do **not** retry on failure: delivery is
at-least-once, and a duplicate `pre_lock` would spend up to another 25 credits
that cannot be bought back, so a failed odds slot alarms and waits for a person
rather than retrying itself.

All five run in `.github/workflows/odds.yml` (see
`docs/automation.md`), kept in one workflow so they share a single
`odds-ledger` concurrency group — the ledger's `BEGIN IMMEDIATE` atomicity
only holds within one filesystem, so two runners each restoring their own
copy could otherwise both pass the guard. Two further guards sit in front
of every slot: an off-season check, and a `--dry-run` preflight that prices
the job using only the free `/events` endpoint. None of that makes a re-run
free — unlike every nflverse/sleeper command, running one of these again
spends credits whether or not the market moved.

`props` refuses to run before Wednesday (props open Wed–Thu); `--force`
overrides that weekday check only, never the budget. `--dry-run` runs the
same event-selection walk `props`/`pre_lock` would and prints the credit
estimate without issuing anything — the inverse of a bypass flag, not one.

## The unknown-reset-day procedure

The quota resets on the subscription anniversary, not the 1st of the
calendar month. Until `ODDS_QUOTA_RESET_DAY` is set in `.env`,
`espn_ff/odds/ledger.py:current_billing_period` treats the period as the
calendar month **and** `effective_quota()` additionally caps at
`ODDS_SAFETY_CAP = 400` so an early reset can't cause an overrun. Every
`credits` and `odds` command prints a warning while this is unset.

To observe it: after a run, note `x-requests-used` (visible via
`sqlite3 data/odds/ledger.db 'select header_used from credit_ledger_entry
order by id desc limit 1'`) and the date. Watch for that number dropping
unexpectedly between two consecutive runs — that drop is the reset. Record
the day-of-month it happened on and set `ODDS_QUOTA_RESET_DAY` in `.env`;
from then on the ledger uses the real 500-credit quota and the real
anniversary period instead of the calendar-month approximation.

## Runbook: when the guard fires

`BudgetExceeded` means exactly one of two things, and the message says
which:

- **Period budget exceeded** (`spent + est > quota - floor`): the
  billing-period reserve is gone. Nothing to do but wait for the reset —
  there is no override. Check `credits` for the current state.
- **Run budget exceeded** (`spent_in_run + est > run_budget`): that job's
  own per-invocation ceiling was hit, independent of the period. The
  `job_run` row's `aborted_reason` records why; the prior snapshot is left
  in place and `last_run.json` for that job is written with `stale=true`.
  Re-running the same job later in the same billing period is fine as
  long as period budget remains — this is a per-run cap, not a lockout.

Either way, a raised `BudgetExceeded` means the job aborted with nothing
issued for whatever it didn't get to — check the job's own return value
and `last_run.json` to see exactly what was skipped. Silent degradation
would be worse than a visible gap here, since a stale projection looks
identical to a fresh one until you check `captured_at`.
