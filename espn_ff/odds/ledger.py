"""The credit ledger: the one enforced invariant this whole package exists
to protect. 500 credits per billing period, no override anywhere.

SQLite at data/odds/ledger.db (stdlib sqlite3), not the nflverse
manifest.json pattern -- a JSON file with a read-modify-write cycle across
two processes can lose a concurrent update; `guard` needs the read, the
budget check, and the ledger append to happen as one atomic unit, which is
exactly what `BEGIN IMMEDIATE` gives an sqlite writer. Three tables, created
on first use: `credit_ledger_entry` (one row per request, estimated then
reconciled), `credit_period_state` (one row per billing period, tracking
both an optimistic running estimate and the API's own authoritative
header), and `job_run` (one row per scheduled job invocation, enforcing a
per-run budget independent of the period budget).

This module has no dependencies on the rest of the package, which is why
`OddsError` -- the base exception client.py, markets.py and everything else
here raises -- is defined here rather than in client.py: client.py already
needs to import `guard`/`reconcile` from this module, and defining the
shared error in client.py would make that an import cycle.

There is no override parameter anywhere in this file. A caller that cannot
determine spend does not get to guess -- any read failure must propagate
so the request never happens.
"""

import datetime as dt
import json
import sqlite3
import uuid
from pathlib import Path

from .. import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS credit_ledger_entry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    billing_period TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    event_id TEXT,
    markets_requested TEXT NOT NULL,
    regions TEXT NOT NULL,
    estimated_cost INTEGER NOT NULL,
    actual_cost INTEGER,
    header_used INTEGER,
    header_remaining INTEGER,
    status TEXT NOT NULL,
    http_status INTEGER,
    job_run_id TEXT
);

CREATE TABLE IF NOT EXISTS credit_period_state (
    billing_period TEXT PRIMARY KEY,
    period_start TEXT,
    period_end TEXT,
    spent_estimated INTEGER NOT NULL DEFAULT 0,
    spent_authoritative INTEGER NOT NULL DEFAULT 0,
    last_header_at TEXT
);

CREATE TABLE IF NOT EXISTS job_run (
    job_run_id TEXT PRIMARY KEY,
    job_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    run_budget INTEGER NOT NULL,
    spent_in_run INTEGER NOT NULL DEFAULT 0,
    aborted_reason TEXT
);
"""

# Only this job may request priority="critical" -- the Sunday pre-lock
# pull, which alone may draw the RESERVE down to zero. guard() asserts on
# this by name; nothing else can opt in.
CRITICAL_JOB = "pre_lock"

# Divergence between our computed remaining and the API's own header that
# triggers the "this key is in use elsewhere" log line.
DIVERGENCE_THRESHOLD = 5


class OddsError(RuntimeError):
    """Base exception for the whole espn_ff.odds package."""


class BudgetExceeded(OddsError):
    """A request was refused by the guard -- either the period or the
    per-run budget would have been exceeded. There is no override."""


def _utcnow():
    return dt.datetime.now(dt.timezone.utc)


def _iso_now():
    return _utcnow().isoformat()


def open_db(path=None):
    path = Path(path) if path else config.ODDS_LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), isolation_level=None, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    return con


def current_billing_period(clock=None):
    """The quota resets on the subscription anniversary, not the 1st of the
    calendar month -- config.odds_quota_reset_day() (ODDS_QUOTA_RESET_DAY)
    is None until that day is observed and configured, in which case the
    period is the calendar month and effective_quota() additionally caps at
    ODDS_SAFETY_CAP so an early reset can't cause an overrun.
    """
    now = (clock or _utcnow)()
    reset_day = config.odds_quota_reset_day()
    if reset_day is None:
        return now.strftime("%Y-%m")

    if now.day >= reset_day:
        anchor = now
    else:
        anchor = now.replace(day=1) - dt.timedelta(days=1)

    try:
        period_start = anchor.replace(day=reset_day)
    except ValueError:
        # reset_day (e.g. 31) doesn't exist in this month -- fall back to
        # the last day of the anchor month rather than raising.
        next_month = anchor.replace(day=28) + dt.timedelta(days=4)
        period_start = next_month - dt.timedelta(days=next_month.day)

    return period_start.strftime("%Y-%m-%d")


def effective_quota():
    return config.ODDS_QUOTA if config.odds_quota_reset_day() is not None else config.ODDS_SAFETY_CAP


def start_run(con, job_name, run_budget):
    job_run_id = uuid.uuid4().hex
    con.execute(
        "INSERT INTO job_run (job_run_id, job_name, started_at, run_budget, spent_in_run, aborted_reason) "
        "VALUES (?, ?, ?, ?, 0, NULL)",
        (job_run_id, job_name, _iso_now(), run_budget),
    )
    return job_run_id


def finish_run(con, job_run_id, aborted_reason=None):
    if aborted_reason is not None:
        con.execute(
            "UPDATE job_run SET aborted_reason = ? WHERE job_run_id = ?", (aborted_reason, job_run_id)
        )


def guard(con, request, priority, job_run_id):
    """The one gate every guarded request must pass through.

    `request` is a dict with `endpoint`, `estimated_cost`, and optionally
    `event_id`, `markets_requested` (list), `regions`. Runs the read, the
    budget check, and the ledger append inside one `BEGIN IMMEDIATE`
    transaction, so two concurrent jobs cannot both pass on stale state.
    Raises BudgetExceeded (recording `aborted_reason` on the job_run row
    first) when either the per-run or the period budget would be exceeded.
    Any other failure -- an unknown job_run_id, a bad priority, a broken
    connection -- propagates rather than defaulting to "allowed".
    """
    if priority not in ("normal", "critical"):
        raise OddsError(f"priority must be 'normal' or 'critical', got {priority!r}")

    period = current_billing_period()
    con.execute("BEGIN IMMEDIATE")

    job_row = con.execute(
        "SELECT job_name, run_budget, spent_in_run FROM job_run WHERE job_run_id = ?", (job_run_id,)
    ).fetchone()
    if job_row is None:
        con.execute("ROLLBACK")
        raise OddsError(f"unknown job_run_id {job_run_id!r} -- call start_run() first")
    if priority == "critical" and job_row["job_name"] != CRITICAL_JOB:
        con.execute("ROLLBACK")
        raise OddsError(f"priority='critical' is reserved for the {CRITICAL_JOB!r} job")

    state_row = con.execute(
        "SELECT spent_estimated, spent_authoritative FROM credit_period_state WHERE billing_period = ?",
        (period,),
    ).fetchone()
    spent_estimated = state_row["spent_estimated"] if state_row else 0
    spent_authoritative = state_row["spent_authoritative"] if state_row else 0
    spent = max(spent_estimated, spent_authoritative)

    est = int(request["estimated_cost"])
    quota = effective_quota()
    floor = 0 if priority == "critical" else config.ODDS_RESERVE

    if job_row["spent_in_run"] + est > job_row["run_budget"]:
        reason = (
            f"run budget exceeded: spent_in_run={job_row['spent_in_run']} + est={est} "
            f"> run_budget={job_row['run_budget']}"
        )
        con.execute("UPDATE job_run SET aborted_reason = ? WHERE job_run_id = ?", (reason, job_run_id))
        con.execute("COMMIT")
        raise BudgetExceeded(reason)

    if spent + est > quota - floor:
        reason = (
            f"period budget exceeded: spent={spent} + est={est} > {quota - floor} "
            f"(quota={quota}, floor={floor}, priority={priority})"
        )
        con.execute("UPDATE job_run SET aborted_reason = ? WHERE job_run_id = ?", (reason, job_run_id))
        con.execute("COMMIT")
        raise BudgetExceeded(reason)

    now = _iso_now()
    cur = con.execute(
        "INSERT INTO credit_ledger_entry "
        "(ts, billing_period, endpoint, event_id, markets_requested, regions, estimated_cost, status, job_run_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'estimated', ?)",
        (
            now,
            period,
            request["endpoint"],
            request.get("event_id"),
            json.dumps(list(request.get("markets_requested") or [])),
            request.get("regions", "us"),
            est,
            job_run_id,
        ),
    )
    entry_id = cur.lastrowid

    if state_row is None:
        con.execute(
            "INSERT INTO credit_period_state "
            "(billing_period, period_start, period_end, spent_estimated, spent_authoritative, last_header_at) "
            "VALUES (?, ?, NULL, ?, 0, NULL)",
            (period, now, est),
        )
    else:
        con.execute(
            "UPDATE credit_period_state SET spent_estimated = spent_estimated + ? WHERE billing_period = ?",
            (est, period),
        )

    con.execute("UPDATE job_run SET spent_in_run = spent_in_run + ? WHERE job_run_id = ?", (est, job_run_id))
    con.execute("COMMIT")
    return entry_id


def reconcile(con, entry_id, response=None, exc=None):
    """Adopt the response headers as ground truth, or -- on a network error
    or a response missing the usage headers -- assume the estimated cost
    was charged anyway (`failed_assumed_charged`). Either way this is the
    only place `spent_estimated` and `spent_authoritative` move after
    `guard` made its initial reservation. Returns the resulting status.
    """
    con.execute("BEGIN IMMEDIATE")
    entry = con.execute(
        "SELECT billing_period, estimated_cost, job_run_id FROM credit_ledger_entry WHERE id = ?",
        (entry_id,),
    ).fetchone()
    if entry is None:
        con.execute("ROLLBACK")
        raise OddsError(f"no ledger entry with id {entry_id}")

    headers = getattr(response, "headers", None) if response is not None else None
    if headers is not None and "x-requests-last" in headers and "x-requests-used" in headers:
        actual_cost = int(headers["x-requests-last"])
        header_used = int(headers["x-requests-used"])
        header_remaining = int(headers["x-requests-remaining"]) if "x-requests-remaining" in headers else None
        status = "reconciled"
        http_status = getattr(response, "status_code", None)
    else:
        actual_cost = entry["estimated_cost"]
        header_used = None
        header_remaining = None
        status = "failed_assumed_charged"
        http_status = getattr(response, "status_code", None) if response is not None else None

    con.execute(
        "UPDATE credit_ledger_entry SET actual_cost = ?, header_used = ?, header_remaining = ?, "
        "status = ?, http_status = ? WHERE id = ?",
        (actual_cost, header_used, header_remaining, status, http_status, entry_id),
    )

    delta = actual_cost - entry["estimated_cost"]
    now = _iso_now()
    con.execute(
        "UPDATE credit_period_state SET spent_estimated = spent_estimated + ?, "
        "spent_authoritative = MAX(spent_authoritative, ?), last_header_at = ? WHERE billing_period = ?",
        (delta, header_used or 0, now, entry["billing_period"]),
    )
    if delta and entry["job_run_id"]:
        con.execute(
            "UPDATE job_run SET spent_in_run = spent_in_run + ? WHERE job_run_id = ?",
            (delta, entry["job_run_id"]),
        )

    if header_remaining is not None:
        row = con.execute(
            "SELECT spent_estimated, spent_authoritative FROM credit_period_state WHERE billing_period = ?",
            (entry["billing_period"],),
        ).fetchone()
        governing_spent = max(row["spent_estimated"], row["spent_authoritative"])
        computed_remaining = effective_quota() - governing_spent
        if abs(header_remaining - computed_remaining) > DIVERGENCE_THRESHOLD:
            print(
                f"  [error] odds ledger diverges from the API's own header by "
                f"{abs(header_remaining - computed_remaining)} credits -- this key is likely in use "
                f"elsewhere. header_remaining={header_remaining} computed_remaining={computed_remaining}"
            )

    con.execute("COMMIT")
    return status


def state(con, period=None):
    period = period or current_billing_period()
    row = con.execute(
        "SELECT spent_estimated, spent_authoritative FROM credit_period_state WHERE billing_period = ?",
        (period,),
    ).fetchone()
    spent_estimated = row["spent_estimated"] if row else 0
    spent_authoritative = row["spent_authoritative"] if row else 0
    spent = max(spent_estimated, spent_authoritative)
    quota = effective_quota()
    remaining = quota - spent
    return {
        "billing_period": period,
        "spent": spent,
        "spent_estimated": spent_estimated,
        "spent_authoritative": spent_authoritative,
        "quota": quota,
        "remaining": remaining,
        "warn": remaining < config.ODDS_WARN_THRESHOLD,
    }
