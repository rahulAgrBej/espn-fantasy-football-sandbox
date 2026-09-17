"""espn_ff.cli exit codes.

An unattended runner is alerted on from the exit status alone, so the two
predictable, actionable failures get their own code rather than sharing 1
with every transient outage:

  2  the Odds credit guard declined to spend -- expected, nothing issued
  3  ESPN session cookies expired -- a person must re-paste them

Both subclass a broader error (BudgetExceeded < OddsError,
PrivateLeagueError < EspnError), so the distinction lives entirely in the
handler ordering in cli.py. These tests pin that ordering.

`summarize` is the one deliberate exception to all of it: it returns 0 for
every failure it knows how to have, because a summary is additive and
nothing it can fail at is worth failing a run over. GeminiError is
deliberately absent from main()'s except-chain for that reason -- the
command catches it itself. The last block of tests here pins that.
"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import requests

from espn_ff import cli
from espn_ff.ai.client import GeminiError
from espn_ff.client import EspnError, PrivateLeagueError
from espn_ff.nflverse.client import NflverseError
from espn_ff.odds.ledger import BudgetExceeded, OddsError
from espn_ff.report import payload as report_payload


def _run(monkeypatch, exc):
    """Invoke main() with a command that raises `exc`, bypassing the network."""
    def boom(client, args):
        raise exc

    monkeypatch.setitem(cli.COMMANDS, "credits", boom)
    # EspnClient's constructor only reads config/cookies, no I/O.
    return cli.main(["credits"])


def test_budget_exceeded_is_its_own_exit_code(monkeypatch):
    assert cli.EXIT_BUDGET == 2
    code = _run(monkeypatch, BudgetExceeded("period budget exceeded: spent=498 + est=4 > 460"))
    assert code == cli.EXIT_BUDGET


def test_expired_cookies_are_their_own_exit_code(monkeypatch):
    assert cli.EXIT_AUTH == 3
    code = _run(monkeypatch, PrivateLeagueError("401 from https://lm-api-reads.fantasy.espn.com/..."))
    assert code == cli.EXIT_AUTH


@pytest.mark.parametrize(
    "exc",
    [
        EspnError("HTTP 503 after 4 attempts"),
        NflverseError("release asset missing"),
        OddsError("ODDS_API_KEY not set"),
    ],
)
def test_real_failures_stay_exit_1(monkeypatch, exc):
    """A plain EspnError is a transient outage, not something to page on."""
    assert _run(monkeypatch, exc) == cli.EXIT_ERROR


def test_the_special_cases_still_subclass_their_broader_errors():
    # The distinction is in cli.py's handler ordering, not in the hierarchy.
    # If either of these stops holding, the except-branch order is what
    # breaks, and the specific code would silently degrade to 1.
    assert issubclass(BudgetExceeded, OddsError)
    assert issubclass(PrivateLeagueError, EspnError)


def test_every_exit_code_is_distinct():
    codes = [cli.EXIT_OK, cli.EXIT_ERROR, cli.EXIT_BUDGET, cli.EXIT_AUTH]
    assert len(set(codes)) == len(codes), codes


def test_success_is_zero(monkeypatch):
    monkeypatch.setitem(cli.COMMANDS, "credits", lambda client, args: cli.EXIT_OK)
    assert cli.main(["credits"]) == cli.EXIT_OK


def _report_stub(markdown):
    """A stand-in for a day module's `build`, returning what the real ones
    return: the markdown plus the structured payload cmd_report writes as
    JSON. A bare string would make these dispatch tests pass against a
    cmd_report that had lost the JSON half entirely."""
    def stub(season, week, team_id=None):
        return report_payload.RenderedReport(
            markdown,
            {
                "header": report_payload.header_block(
                    "Stub", week, "a stub report", None, 1_760_000_000
                ),
                "sections": [report_payload.prose_section("stub", "Stub", ["body"])],
            },
        )
    return stub


# Captured before the autouse stub below replaces it, so the tests that
# exercise the refresh can put the real one back by name rather than by
# monkeypatch.undo() -- which would also drop whatever else the test had
# already patched.
_REAL_REFRESH = cli._refresh_espn_for_report


@pytest.fixture(autouse=True)
def no_espn_refresh(monkeypatch):
    """`cmd_report` re-pulls ESPN's live views before it builds, which means
    a bare `cli.main(["report", ...])` in a test reaches the network and
    rewrites data/out/. Autouse rather than a flag on each call site so a
    report test added later is protected without anyone remembering to.

    The refresh itself is exercised deliberately, further down, against a
    stubbed `cmd_export`."""
    monkeypatch.setattr(cli, "_refresh_espn_for_report", lambda client, args: None)


def test_report_day_tuesday_dispatches_to_the_tuesday_builder(monkeypatch, tmp_path):
    """report --day tuesday used to hit cmd_report's "not implemented yet"
    guard, since REPORT_DAYS only ever held "monday". Pins that REPORTS
    now routes tuesday to report_tuesday.build rather than falling
    through to that error path."""
    stub = _report_stub("stub tuesday report\n")
    monkeypatch.setitem(cli.REPORTS, "tuesday", (stub, "tuesday", "week-in-review"))
    monkeypatch.setattr(cli.config, "PROJECT_ROOT", tmp_path)

    code = cli.main(["report", "--day", "tuesday", "--week", "2"])

    assert code == cli.EXIT_OK
    written = list(tmp_path.rglob("*-tuesday-week-in-review.md"))
    assert len(written) == 1
    assert written[0].read_text() == "stub tuesday report\n"


def test_report_filename_uses_et_date_not_runner_local_date(monkeypatch, tmp_path):
    """A UTC runner can already be into the next calendar day while ET
    (what header_lines stamps `**Rendered**` with) is still on the
    previous one. cmd_report used to name the file from `date.today()`
    (runner-local), so a render crossing that boundary landed beside the
    stale file instead of replacing it. Pins that the filename is now
    derived from `datetime.now(ET)` instead."""
    stub = _report_stub("stub tuesday report\n")
    monkeypatch.setitem(cli.REPORTS, "tuesday", (stub, "tuesday", "week-in-review"))
    monkeypatch.setattr(cli.config, "PROJECT_ROOT", tmp_path)

    # 2026-09-16 02:00 UTC is 2026-09-15 22:00 ET (EDT, UTC-4) -- the two
    # calendar dates disagree.
    utc_evening = datetime(2026, 9, 16, 2, 0, tzinfo=ZoneInfo("UTC"))

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return utc_evening.astimezone(tz) if tz else utc_evening

    monkeypatch.setattr(cli, "datetime", FixedDatetime)

    code = cli.main(["report", "--day", "tuesday", "--week", "2"])

    assert code == cli.EXIT_OK
    written = list(tmp_path.rglob("*-tuesday-week-in-review.md"))
    assert len(written) == 1
    assert written[0].name.startswith("2026-09-15-")


def test_report_day_saturday_dispatches_to_the_saturday_builder(monkeypatch, tmp_path):
    """report --day saturday must route to report_saturday.build, the same
    way test_report_day_tuesday_dispatches_to_the_tuesday_builder pins
    tuesday's routing."""
    stub = _report_stub("stub saturday report\n")
    monkeypatch.setitem(cli.REPORTS, "saturday", (stub, "saturday", "contingency-check"))
    monkeypatch.setattr(cli.config, "PROJECT_ROOT", tmp_path)

    code = cli.main(["report", "--day", "saturday", "--week", "3"])

    assert code == cli.EXIT_OK
    written = list(tmp_path.rglob("*-saturday-contingency-check.md"))
    assert len(written) == 1
    assert written[0].read_text() == "stub saturday report\n"


def test_report_day_sunday_dispatches_to_the_sunday_builder(monkeypatch, tmp_path):
    """report --day sunday must route to report_sunday.build, the same way
    test_report_day_saturday_dispatches_to_the_saturday_builder pins
    saturday's routing."""
    stub = _report_stub("stub sunday report\n")
    monkeypatch.setitem(cli.REPORTS, "sunday", (stub, "sunday", "pre-lock-call"))
    monkeypatch.setattr(cli.config, "PROJECT_ROOT", tmp_path)

    code = cli.main(["report", "--day", "sunday", "--week", "3"])

    assert code == cli.EXIT_OK
    written = list(tmp_path.rglob("*-sunday-pre-lock-call.md"))
    assert len(written) == 1
    assert written[0].read_text() == "stub sunday report\n"


# --- summarize: always 0 --------------------------------------------------


REPORT = """\
# Waiver wire and opening market -- 2026 week 2

**Covers** week 2's waiver window
**Week 2** Tue 2026-09-15 03:00 - Tue 2026-09-22 03:00 ET
**Rendered** Tue 2026-09-15 21:18 ET
"""


@pytest.fixture
def summarize_root(monkeypatch, tmp_path):
    """A project root with one unsummarized report and no credential in the
    environment. cmd_summarize derives every default path from
    config.PROJECT_ROOT, so pointing that at tmp_path is enough to keep the
    whole command inside the sandbox."""
    monkeypatch.setattr(cli.config, "PROJECT_ROOT", tmp_path)
    # load_dotenv would otherwise read a real .env off the developer's disk
    # and hand this test a live key.
    monkeypatch.setattr(cli.config, "load_dotenv", lambda path=None: None)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    report = tmp_path / ".cache" / "s3-reports" / "2026" / "week-02" / "2026-09-15-tuesday-waiver-wire.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(REPORT)
    return tmp_path


def test_summarize_returns_zero_with_no_api_key(summarize_root, capsys):
    """An unconfigured model must not fail a run. The report is already
    written and mirrored; the summary is the additive part."""
    assert cli.main(["summarize"]) == cli.EXIT_OK
    assert "GEMINI_API_KEY not set" in capsys.readouterr().err


def test_summarize_returns_zero_with_nothing_to_summarize(monkeypatch, tmp_path):
    """The steady state: the AWS backstop firing 20 minutes after the
    workflow_run event already did the work."""
    monkeypatch.setattr(cli.config, "PROJECT_ROOT", tmp_path)

    assert cli.main(["summarize"]) == cli.EXIT_OK


def test_summarize_spends_nothing_when_there_is_nothing_to_do(monkeypatch, tmp_path):
    """Not merely "returns 0" -- it must not construct a client at all, or
    an idempotent no-op run would still fail on a missing credential."""
    monkeypatch.setattr(cli.config, "PROJECT_ROOT", tmp_path)

    def never(*args, **kwargs):
        raise AssertionError("a client was constructed for a run with no work")

    monkeypatch.setattr(cli.ai_summarize, "GeminiClient", never)
    assert cli.main(["summarize"]) == cli.EXIT_OK


@pytest.mark.parametrize(
    "exc",
    [
        GeminiError("HTTP 503 after 4 attempts"),
        GeminiError("generation stopped with finishReason=MAX_TOKENS"),
        requests.ConnectionError("name or service not known"),
    ],
)
def test_summarize_returns_zero_when_the_client_dies(summarize_root, monkeypatch, exc):
    class DeadClient:
        model = "dead"

        def __init__(self, *args, **kwargs):
            pass

        def generate(self, *args, **kwargs):
            raise exc

    monkeypatch.setattr(cli.ai_summarize, "GeminiClient", DeadClient)
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-key")

    assert cli.main(["summarize"]) == cli.EXIT_OK


def test_summarize_returns_zero_when_the_client_cannot_even_be_built(summarize_root, monkeypatch):
    def boom(*args, **kwargs):
        raise GeminiError("GEMINI_API_KEY not set -- add it to .env")

    monkeypatch.setattr(cli.ai_summarize, "GeminiClient", boom)

    assert cli.main(["summarize"]) == cli.EXIT_OK


def test_gemini_errors_are_not_in_mains_except_chain():
    """The departure is scoped to cmd_summarize, deliberately. If GeminiError
    were ever added to main()'s handler chain it would silently start
    mapping to exit 1 for every other command too."""
    assert not issubclass(GeminiError, (EspnError, NflverseError, OddsError))


def test_report_writes_the_json_twin_beside_the_markdown(monkeypatch, tmp_path):
    """One `build` call, two artifacts. The JSON lands in a sibling
    *directory*, not a sibling file: report.yml commits `reports/` wholesale
    and s3_sync.sh mirrors that tree with --delete, so a .json inside it
    would be committed and would inherit the wrong sync semantics."""
    monkeypatch.setitem(cli.REPORTS, "tuesday", (_report_stub("stub\n"), "tuesday", "week-in-review"))
    monkeypatch.setattr(cli.config, "PROJECT_ROOT", tmp_path)

    assert cli.main(["report", "--day", "tuesday", "--week", "2"]) == cli.EXIT_OK

    md = next(iter(tmp_path.rglob("*-tuesday-week-in-review.md")))
    written = list(tmp_path.rglob("*-tuesday-week-in-review.json"))
    assert len(written) == 1
    assert written[0].parent == tmp_path / "reports-json" / "2026" / "week-02"
    assert md.parent == tmp_path / "reports" / "2026" / "week-02"
    assert written[0].stem == md.stem
    # Nothing JSON-shaped may appear under the --delete-mirrored tree.
    assert not list((tmp_path / "reports").rglob("*.json"))


# ---- report: the ESPN refresh must never cost us the report ---------------
#
# cmd_report fetches before it builds so the roster it reads is this run's,
# not the last collection slot's (the 2026-09-17 incident: a Wednesday-
# afternoon trade, still absent from Thursday's 11:01 ET report). That put a
# network call in front of the one workflow that writes back to the repo --
# and report.yml's commit step carries no `if: always()`, so any non-zero
# return here loses the report entirely. A failed refresh must degrade, never
# abort.


def _refreshing_report(monkeypatch, tmp_path, exc):
    """Run `report` for real -- refresh included -- with `cmd_export` raising."""
    def boom(client, args):
        raise exc

    monkeypatch.setattr(cli, "_refresh_espn_for_report", _REAL_REFRESH)
    monkeypatch.setattr(cli, "cmd_export", boom)
    monkeypatch.setitem(
        cli.REPORTS, "tuesday", (_report_stub("stub\n"), "tuesday", "week-in-review")
    )
    monkeypatch.setattr(cli.config, "PROJECT_ROOT", tmp_path)
    return cli.main(["report", "--day", "tuesday", "--week", "2"])


@pytest.mark.parametrize(
    "exc",
    [
        EspnError("503 from ESPN after 4 attempts"),
        requests.ConnectionError("connection reset"),
    ],
)
def test_report_still_renders_when_the_espn_refresh_fails(monkeypatch, tmp_path, exc):
    assert _refreshing_report(monkeypatch, tmp_path, exc) == cli.EXIT_OK
    assert len(list(tmp_path.rglob("*-tuesday-week-in-review.md"))) == 1
    assert len(list(tmp_path.rglob("*-tuesday-week-in-review.json"))) == 1


def test_expired_cookies_during_a_report_refresh_do_not_become_exit_auth(monkeypatch, tmp_path):
    """The one deliberate hole in main()'s except-chain. PrivateLeagueError
    normally means EXIT_AUTH, because cookies are the one failure a person
    must act on -- but raised *inside a report's refresh* it must not fail
    the run, or an expired cookie costs us every report until someone
    notices. health.yml's probe and espn.yml still surface it as EXIT_AUTH,
    and the report itself says the roster could not be refreshed via
    loaders.roster_read_is_current."""
    code = _refreshing_report(monkeypatch, tmp_path, PrivateLeagueError("401 from ESPN"))
    assert code == cli.EXIT_OK
    assert code != cli.EXIT_AUTH


def test_the_failed_refresh_is_announced_not_swallowed(monkeypatch, tmp_path, capsys):
    """Degrading quietly is how the 2026-09-16 waiver gap went unnoticed."""
    _refreshing_report(monkeypatch, tmp_path, EspnError("503 from ESPN"))
    assert "::warning::ESPN refresh failed" in capsys.readouterr().err


def test_no_espn_refresh_skips_the_fetch_entirely(monkeypatch, tmp_path):
    """The debugging opt-out must not merely tolerate a failing export -- it
    must not call it at all."""
    calls = []
    monkeypatch.setattr(cli, "_refresh_espn_for_report", _REAL_REFRESH)
    monkeypatch.setattr(cli, "cmd_export", lambda client, args: calls.append(1))
    monkeypatch.setitem(
        cli.REPORTS, "tuesday", (_report_stub("stub\n"), "tuesday", "week-in-review")
    )
    monkeypatch.setattr(cli.config, "PROJECT_ROOT", tmp_path)

    assert cli.main(
        ["report", "--day", "tuesday", "--week", "2", "--no-espn-refresh"]
    ) == cli.EXIT_OK
    assert calls == []


def test_report_json_embeds_the_markdown_verbatim(monkeypatch, tmp_path):
    """The no-information-loss guarantee, end to end: whatever the section
    structuring did not capture is still readable out of the envelope."""
    monkeypatch.setitem(
        cli.REPORTS, "tuesday", (_report_stub("# T\n\nbody\n"), "tuesday", "week-in-review")
    )
    monkeypatch.setattr(cli.config, "PROJECT_ROOT", tmp_path)

    cli.main(["report", "--day", "tuesday", "--week", "2"])

    written = next(iter(tmp_path.rglob("*-tuesday-week-in-review.json")))
    envelope = json.loads(written.read_text())
    assert envelope["markdown"] == "# T\n\nbody\n"
    assert envelope["schema_version"] == report_payload.SCHEMA_VERSION
    assert envelope["day"] == "tuesday" and envelope["week"] == 2
    assert envelope["related"]["markdown_path"] == (
        f"reports/2026/week-02/{written.stem}.md"
    )
