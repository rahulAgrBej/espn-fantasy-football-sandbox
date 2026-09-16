"""Guards against the hand-maintained links between workflow YAML and the
Python it dispatches.

Three of them, all maintained by hand in separate files with nothing else
checking they agree:

1.  report.yml's workflow_dispatch options vs espn_ff.cli.REPORTS. A day
    present in one but not the other fails loudly (a 422 from GitHub, or
    cmd_report's "not implemented yet" guard) rather than silently, but only
    once someone notices -- this is what catches it at PR time instead.

2.  summary.yml's `workflow_run.workflows` vs report.yml's `name:`. That one
    is worse: GitHub matches the trigger on the workflow's *name*, not its
    filename, and a name that matches nothing is not an error. The event
    simply never fires, summary.yml keeps working off its AWS backstop, and
    the only symptom is summaries arriving 20 minutes late forever.

3.  infra/scheduler.yaml's `Input: '{"day":"<key>"}'` occurrences vs
    espn_ff.cli.REPORTS. Adding a report day means touching cli.py,
    report.yml and scheduler.yaml by hand in three separate files; the first
    two guards above catch a drift between the first two, but nothing
    caught a report day with no AWS schedule at all until this one.
"""

import re
from pathlib import Path

from espn_ff import cli

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
REPORT_YML = WORKFLOWS / "report.yml"
SUMMARY_YML = WORKFLOWS / "summary.yml"
SCHEDULER_YAML = Path(__file__).resolve().parent.parent / "infra" / "scheduler.yaml"


def _options_in_report_yml():
    text = REPORT_YML.read_text()
    match = re.search(r"options:\s*\[([^\]]*)\]", text)
    assert match, "report.yml: no `options: [...]` line found under workflow_dispatch.inputs.day"
    return {item.strip() for item in match.group(1).split(",")}


def _workflow_name(path):
    match = re.search(r"^name:\s*(\S+)\s*$", path.read_text(), re.MULTILINE)
    assert match, f"{path.name}: no top-level `name:` found"
    return match.group(1)


def _watched_workflows_in_summary_yml():
    text = SUMMARY_YML.read_text()
    match = re.search(r"workflow_run:\s*\n\s*workflows:\s*\[([^\]]*)\]", text)
    assert match, "summary.yml: no `workflows: [...]` list found under on.workflow_run"
    return {item.strip() for item in match.group(1).split(",")}


def test_report_yml_options_match_cli_reports():
    assert _options_in_report_yml() == set(cli.REPORTS)


def _report_days_in_scheduler_yaml():
    text = SCHEDULER_YAML.read_text()
    return set(re.findall(r'Input:\s*\'\{"day":"([^"]+)"\}\'', text))


def _summary_schedule_names_in_scheduler_yaml():
    text = SCHEDULER_YAML.read_text()
    return set(re.findall(r"^\s*Name:\s*summary-([a-z-]+)\s*$", text, re.MULTILINE))


def test_scheduler_yaml_has_a_report_schedule_per_cli_report():
    """infra/scheduler.yaml is hand-linked to cli.REPORTS the same way
    report.yml is, but nothing checked it until now -- a report day could
    ship with a working `--day` flag and a working workflow option and
    still never be dispatched by AWS at all."""
    assert _report_days_in_scheduler_yaml() == set(cli.REPORTS)


def test_scheduler_yaml_has_a_summary_backstop_per_report_schedule():
    """Same class of silent drift as the report-schedule guard above: a new
    report day needs its own summary-* backstop schedule too."""
    assert _summary_schedule_names_in_scheduler_yaml() == set(cli.REPORTS)


def test_summary_yml_watches_report_yml_by_its_actual_name():
    """The trigger names a workflow, not a file. If report.yml is ever
    renamed, this is the only thing that notices."""
    assert _watched_workflows_in_summary_yml() == {_workflow_name(REPORT_YML)}


def test_summary_yml_gates_on_the_triggering_run_having_succeeded():
    """workflow_run fires on failure and cancellation too. Without the
    conclusion gate, a failed report run would still kick off a summary
    pass -- harmless today only because the job is idempotent, and exactly
    the kind of thing that stops being harmless later."""
    text = SUMMARY_YML.read_text()

    assert "github.event.workflow_run.conclusion == 'success'" in text
    assert "github.event_name != 'workflow_run'" in text, (
        "the dispatch path carries no workflow_run context, so the gate must let it through"
    )


def test_summary_yml_declares_the_environment_that_gates_aws_access():
    """Every job that touches S3 must declare `environment: gh_env` -- both
    the secret lookup and the assume-role call fail without it. See
    docs/automation.md's "Credentials"."""
    assert re.search(r"^\s*environment:\s*gh_env\s*$", SUMMARY_YML.read_text(), re.MULTILINE)


def test_summary_yml_asks_for_no_write_access_to_the_repo():
    """Unlike report.yml it commits nothing; summaries/ is gitignored and
    the only output goes to S3."""
    # Matched as a YAML key line, not as a substring: this file's own header
    # comment explains why it does NOT take contents: write, and a naive
    # substring search finds that sentence.
    text = SUMMARY_YML.read_text()

    assert re.search(r"^\s*contents:\s*read\s*$", text, re.MULTILINE)
    assert not re.search(r"^\s*contents:\s*write\s*$", text, re.MULTILINE)


def test_summary_yml_pulls_its_inputs_from_s3_before_running():
    """S3 is the source of truth for this job. The checkout is present (every
    workflow checks out) but is only a fallback -- if these pulls were ever
    dropped, `summarize` would silently run off the checkout and tag every
    envelope source=local."""
    text = SUMMARY_YML.read_text()

    for verb in ("restore-out teams roster-slots weekly-rosters", "pull-reports", "pull-summaries"):
        assert f"s3_sync.sh {verb}" in text, f"summary.yml no longer runs {verb}"

    run_step = text.index("name: Summarize")
    for verb in ("pull-reports", "pull-summaries"):
        assert text.index(f"s3_sync.sh {verb}") < run_step, f"{verb} must precede the Python run"


def test_summary_yml_restores_the_export_the_news_prompt_reads():
    """weekly-rosters is the news layer's entire input. Drop it from
    restore-out and every grounded call is skipped with a news_error, on
    every report, forever -- while the workflow still goes green and the
    summaries still land. Nothing else would notice."""
    text = SUMMARY_YML.read_text()

    restore = next(
        line for line in text.splitlines() if "s3_sync.sh restore-out" in line
    )
    assert "weekly-rosters" in restore


def test_summary_yml_pushes_summaries_append_only():
    """--delete over summaries/ would wipe the bucket's history every run,
    because the local tree holds only what this run produced."""
    text = SUMMARY_YML.read_text()

    assert "s3_sync.sh sync-summaries" in text
    # Comment lines stripped first: this file explains in prose why --delete
    # would be destructive here, and a naive search finds that sentence.
    commands = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "--delete" not in commands
