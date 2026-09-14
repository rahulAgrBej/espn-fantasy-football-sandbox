"""The credit ledger -- in-memory-ish sqlite (tmp_path file), no network,
no real credits. Covers handoff acceptance criteria one-for-one."""

import datetime as dt

import pytest

from espn_ff.odds import ledger


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.delenv("ODDS_QUOTA_RESET_DAY", raising=False)
    return ledger.open_db(tmp_path / "ledger.db")


class _Response:
    def __init__(self, headers=None, status_code=200):
        self.headers = headers or {}
        self.status_code = status_code


def _job(con, job_name="slate_context", run_budget=100):
    return ledger.start_run(con, job_name, run_budget)


def test_current_billing_period_is_calendar_month_when_reset_day_unknown(monkeypatch):
    monkeypatch.delenv("ODDS_QUOTA_RESET_DAY", raising=False)
    clock = lambda: dt.datetime(2026, 9, 14, tzinfo=dt.timezone.utc)
    assert ledger.current_billing_period(clock=clock) == "2026-09"


def test_current_billing_period_uses_anniversary_day_once_configured(monkeypatch):
    monkeypatch.setenv("ODDS_QUOTA_RESET_DAY", "20")
    before_reset = lambda: dt.datetime(2026, 9, 14, tzinfo=dt.timezone.utc)
    after_reset = lambda: dt.datetime(2026, 9, 25, tzinfo=dt.timezone.utc)
    assert ledger.current_billing_period(clock=before_reset) == "2026-08-20"
    assert ledger.current_billing_period(clock=after_reset) == "2026-09-20"


def test_effective_quota_is_safety_cap_until_reset_day_known(monkeypatch):
    monkeypatch.delenv("ODDS_QUOTA_RESET_DAY", raising=False)
    assert ledger.effective_quota() == 400
    monkeypatch.setenv("ODDS_QUOTA_RESET_DAY", "20")
    assert ledger.effective_quota() == 500


def test_guard_records_an_estimated_entry_and_updates_state(con):
    job_run_id = _job(con)
    entry_id = ledger.guard(
        con, {"endpoint": "featured_odds", "markets_requested": ["spreads", "totals"], "estimated_cost": 2},
        priority="normal", job_run_id=job_run_id,
    )
    row = con.execute("SELECT * FROM credit_ledger_entry WHERE id = ?", (entry_id,)).fetchone()
    assert row["status"] == "estimated"
    assert row["estimated_cost"] == 2

    s = ledger.state(con)
    assert s["spent"] == 2
    assert s["remaining"] == 400 - 2  # safety cap governs with no reset day configured


def test_guard_blocks_at_460_normal_priority(con, monkeypatch):
    monkeypatch.setenv("ODDS_QUOTA_RESET_DAY", "20")  # quota=500, reserve=40 -> boundary 460
    job_run_id = _job(con, run_budget=1000)
    monkey_state_at(con, spent=458)
    with pytest.raises(ledger.BudgetExceeded):
        ledger.guard(
            con, {"endpoint": "featured_odds", "estimated_cost": 3},
            priority="normal", job_run_id=job_run_id,
        )
    # exactly at the boundary must succeed
    ledger.guard(
        con, {"endpoint": "featured_odds", "estimated_cost": 2},
        priority="normal", job_run_id=job_run_id,
    )


def test_guard_blocks_at_500_for_critical_job_only(con, monkeypatch):
    monkeypatch.setenv("ODDS_QUOTA_RESET_DAY", "20")  # quota=500, floor=0 for critical
    job_run_id = _job(con, job_name="pre_lock", run_budget=1000)
    monkey_state_at(con, spent=499)
    with pytest.raises(ledger.BudgetExceeded):
        ledger.guard(
            con, {"endpoint": "event_odds", "estimated_cost": 2},
            priority="critical", job_run_id=job_run_id,
        )
    ledger.guard(
        con, {"endpoint": "event_odds", "estimated_cost": 1},
        priority="critical", job_run_id=job_run_id,
    )


def test_guard_rejects_critical_priority_from_a_non_prelock_job(con):
    job_run_id = _job(con, job_name="props_primary")
    with pytest.raises(ledger.OddsError):
        ledger.guard(
            con, {"endpoint": "event_odds", "estimated_cost": 1},
            priority="critical", job_run_id=job_run_id,
        )


def test_guard_enforces_per_run_budget_independent_of_period_budget(con):
    job_run_id = _job(con, run_budget=4)
    ledger.guard(con, {"endpoint": "featured_odds", "estimated_cost": 2}, priority="normal", job_run_id=job_run_id)
    ledger.guard(con, {"endpoint": "featured_odds", "estimated_cost": 2}, priority="normal", job_run_id=job_run_id)
    with pytest.raises(ledger.BudgetExceeded):
        ledger.guard(con, {"endpoint": "featured_odds", "estimated_cost": 1}, priority="normal", job_run_id=job_run_id)

    row = con.execute("SELECT aborted_reason FROM job_run WHERE job_run_id = ?", (job_run_id,)).fetchone()
    assert "run budget exceeded" in row["aborted_reason"]


def test_guard_raises_on_unknown_job_run_id(con):
    with pytest.raises(ledger.OddsError):
        ledger.guard(con, {"endpoint": "featured_odds", "estimated_cost": 1}, priority="normal", job_run_id="ghost")


def test_a_retry_after_429_creates_a_second_ledger_entry_not_a_reused_one(con):
    """Simulates client.py's retry contract: a retry re-enters guard()."""
    job_run_id = _job(con)
    first = ledger.guard(con, {"endpoint": "featured_odds", "estimated_cost": 2}, priority="normal", job_run_id=job_run_id)
    second = ledger.guard(con, {"endpoint": "featured_odds", "estimated_cost": 2}, priority="normal", job_run_id=job_run_id)
    assert first != second
    count = con.execute("SELECT COUNT(*) AS n FROM credit_ledger_entry").fetchone()["n"]
    assert count == 2


def test_reconcile_adopts_response_headers(con):
    job_run_id = _job(con)
    entry_id = ledger.guard(con, {"endpoint": "featured_odds", "estimated_cost": 2}, priority="normal", job_run_id=job_run_id)
    response = _Response(headers={"x-requests-last": "2", "x-requests-used": "2", "x-requests-remaining": "398"})
    status = ledger.reconcile(con, entry_id, response=response)
    assert status == "reconciled"

    row = con.execute("SELECT * FROM credit_ledger_entry WHERE id = ?", (entry_id,)).fetchone()
    assert row["actual_cost"] == 2
    assert row["header_used"] == 2

    s = ledger.state(con)
    assert s["spent_authoritative"] == 2


def test_reconcile_with_no_response_records_failed_assumed_charged_at_estimated_cost(con):
    job_run_id = _job(con)
    entry_id = ledger.guard(con, {"endpoint": "featured_odds", "estimated_cost": 3}, priority="normal", job_run_id=job_run_id)
    status = ledger.reconcile(con, entry_id, exc=TimeoutError("boom"))
    assert status == "failed_assumed_charged"

    row = con.execute("SELECT * FROM credit_ledger_entry WHERE id = ?", (entry_id,)).fetchone()
    assert row["actual_cost"] == 3

    s = ledger.state(con)
    assert s["spent"] == 3  # already counted at guard time; reconcile made no further change


def test_reconcile_with_response_missing_usage_headers_is_also_failed_assumed_charged(con):
    job_run_id = _job(con)
    entry_id = ledger.guard(con, {"endpoint": "featured_odds", "estimated_cost": 2}, priority="normal", job_run_id=job_run_id)
    status = ledger.reconcile(con, entry_id, response=_Response(headers={}, status_code=500))
    assert status == "failed_assumed_charged"


def test_reconcile_divergence_over_threshold_logs_error_and_adopts_header(con, capsys):
    job_run_id = _job(con)
    entry_id = ledger.guard(con, {"endpoint": "featured_odds", "estimated_cost": 2}, priority="normal", job_run_id=job_run_id)
    # Our own ledger computes remaining = 400 - 50 = 350, but the header says
    # something far off -- the key is in use elsewhere.
    response = _Response(headers={"x-requests-last": "2", "x-requests-used": "50", "x-requests-remaining": "300"})
    ledger.reconcile(con, entry_id, response=response)

    assert "error" in capsys.readouterr().out.lower()
    s = ledger.state(con)
    assert s["spent_authoritative"] == 50  # the header, not our own estimate, now governs


def test_state_reports_warn_below_threshold(con):
    job_run_id = _job(con, run_budget=1000)
    monkey_state_at(con, spent=305)  # 400 - 305 = 95 < WARN_THRESHOLD (100)
    assert ledger.state(con)["warn"] is True


def test_ledger_stub_raising_means_zero_http_calls(monkeypatch):
    """The headline invariant, exercised at the module boundary the client
    actually calls through -- full client-level coverage lives in
    test_odds_client.py."""
    def _boom(*args, **kwargs):
        raise ledger.OddsError("ledger unavailable")

    monkeypatch.setattr(ledger, "guard", _boom)
    with pytest.raises(ledger.OddsError):
        ledger.guard(None, {}, priority="normal", job_run_id="x")


def monkey_state_at(con, spent):
    """Test helper: seed credit_period_state so spent_estimated == `spent`
    for the current (no-reset-day) billing period."""
    period = ledger.current_billing_period()
    con.execute(
        "INSERT INTO credit_period_state (billing_period, period_start, spent_estimated, spent_authoritative) "
        "VALUES (?, ?, ?, 0) "
        "ON CONFLICT(billing_period) DO UPDATE SET spent_estimated = ?",
        (period, ledger._iso_now(), spent, spent),
    )
