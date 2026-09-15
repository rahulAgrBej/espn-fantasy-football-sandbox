"""espn_ff.cli exit codes.

An unattended runner is alerted on from the exit status alone, so the two
predictable, actionable failures get their own code rather than sharing 1
with every transient outage:

  2  the Odds credit guard declined to spend -- expected, nothing issued
  3  ESPN session cookies expired -- a person must re-paste them

Both subclass a broader error (BudgetExceeded < OddsError,
PrivateLeagueError < EspnError), so the distinction lives entirely in the
handler ordering in cli.py. These tests pin that ordering.
"""

import pytest

from espn_ff import cli
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
