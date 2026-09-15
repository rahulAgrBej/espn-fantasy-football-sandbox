"""espn_ff.cli exit codes.

An unattended runner has to tell three outcomes apart from the exit status
alone: it worked, it broke and needs a human, or the Odds credit guard
declined to spend. BudgetExceeded subclasses OddsError, so without a
dedicated branch a correct budget abort would be indistinguishable from
expired ESPN cookies -- and the CI alerting would be noise within a week.
"""

import pytest

from espn_ff import cli
from espn_ff.client import EspnError
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


@pytest.mark.parametrize(
    "exc",
    [
        EspnError("401 AUTH_LEAGUE_NOT_VISIBLE"),
        NflverseError("release asset missing"),
        OddsError("ODDS_API_KEY not set"),
    ],
)
def test_real_failures_stay_exit_1(monkeypatch, exc):
    assert _run(monkeypatch, exc) == cli.EXIT_ERROR


def test_budget_exceeded_is_still_an_odds_error():
    # The distinction is in cli.py's handler ordering, not in the hierarchy --
    # if this ever stops holding, the except-branch order is what breaks.
    assert issubclass(BudgetExceeded, OddsError)


def test_success_is_zero(monkeypatch):
    monkeypatch.setitem(cli.COMMANDS, "credits", lambda client, args: cli.EXIT_OK)
    assert cli.main(["credits"]) == cli.EXIT_OK
