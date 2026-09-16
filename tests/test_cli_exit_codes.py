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

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import requests

from espn_ff import cli
from espn_ff.ai.client import GeminiError
from espn_ff.client import EspnError, PrivateLeagueError
from espn_ff.nflverse.client import NflverseError
from espn_ff.odds.ledger import BudgetExceeded, OddsError


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


def test_report_day_tuesday_dispatches_to_the_tuesday_builder(monkeypatch, tmp_path):
    """report --day tuesday used to hit cmd_report's "not implemented yet"
    guard, since REPORT_DAYS only ever held "monday". Pins that REPORTS
    now routes tuesday to report_tuesday.build rather than falling
    through to that error path."""
    stub = lambda season, week, team_id=None: "stub tuesday report\n"
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
    stub = lambda season, week, team_id=None: "stub tuesday report\n"
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
