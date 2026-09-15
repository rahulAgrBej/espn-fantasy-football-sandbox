"""Guards against report.yml's workflow_dispatch options drifting from
espn_ff.cli.REPORTS -- the two are maintained by hand in separate files and
nothing else checks they agree. A day present in one but not the other fails
loudly (a 422 from GitHub, or cmd_report's "not implemented yet" guard)
rather than silently, but only once someone notices -- this test is what
catches it at PR time instead."""

import re
from pathlib import Path

from espn_ff import cli

WORKFLOW_PATH = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "report.yml"


def _options_in_report_yml():
    text = WORKFLOW_PATH.read_text()
    match = re.search(r"options:\s*\[([^\]]*)\]", text)
    assert match, "report.yml: no `options: [...]` line found under workflow_dispatch.inputs.day"
    return {item.strip() for item in match.group(1).split(",")}


def test_report_yml_options_match_cli_reports():
    assert _options_in_report_yml() == set(cli.REPORTS)
