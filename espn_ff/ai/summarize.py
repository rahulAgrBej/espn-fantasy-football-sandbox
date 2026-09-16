"""Orchestration: discover, prompt, generate, and write the JSON envelope.

The one module here that touches disk. `prompt.py` stays pure, `reports.py`
only reads, `client.py` only talks to the network -- this is where the three
meet.

**S3 is the source of truth here, and the local tree is a backup.** Every
input this module reads comes from the bucket first:

  `reports_dir`    the `reports/` mirror pulled by `s3_sync.sh pull-reports`
                   -- what report.yml's own last step writes, and what the
                   envelope's `report.path` keys name.
  `reports_fallback_dir`
                   the git checkout's `reports/`, read **only** when the
                   mirror turns up empty. See `resolve_source`; the choice
                   is recorded in every envelope as `report.source`.

Summaries need two directories, deliberately not one:

  `summaries_dir`  the read cache of every summary already on S3, pulled by
                   `s3_sync.sh pull-summaries`. Consulted to decide what
                   still needs doing; never written to.
  `out_dir`        `summaries/` in the repo root, holding **only what this
                   run produced**, pushed by `s3_sync.sh sync-summaries`.

Keeping them apart is what lets that push stay append-only and safe.
`summaries/` is gitignored, so `actions/checkout` restores none of its
history; if the push mirrored with `--delete` from a tree holding one run's
output, it would wipe every prior summary in the bucket.
"""

import hashlib
import json
from datetime import datetime
from pathlib import Path

import requests

from .. import config
from ..report.loaders import latest_export
from ..weeks import ET
from . import prompt, reports
from .client import GeminiClient, GeminiError

SCHEMA_VERSION = 1

# Caps how many reports one run will summarise. A first run over a
# backfilled season would otherwise fire one call per report with no
# warning; this bounds it, and `run` logs by name whatever the cap dropped.
# Silence is this repo's documented failure mode (docs/automation.md's
# "a report can succeed and say nothing"), so a silent cap is not an option.
DEFAULT_LIMIT = 4

DOCS = {
    "column_dictionary": "data-sources.md",
    "report_semantics": "report-weekly-schedule.md",
}


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _s3_key(report):
    """The report's key under the bucket's `reports/` prefix, derived from
    the report's own season/week rather than from whichever local directory
    it was read out of -- the envelope should name the durable location, not
    a runner's cache path."""
    return f"reports/{report.season}/week-{report.week:02d}/{report.path.name}"


def _read_doc(docs_dir, name):
    """Doc text, or an explicit marker. A missing doc degrades the summary
    rather than failing the run -- the same skip-on-missing discipline the
    collection side uses -- but it says so in the prompt instead of leaving
    a silent hole the model fills by inference."""
    path = Path(docs_dir) / name
    if not path.exists():
        print(f"  [warning] docs/{name} not found -- prompting without it")
        return f"(docs/{name} was not available to this run.)"
    return path.read_text()


def build_prompt(report, prior_reports, docs_dir):
    """The (system instruction, user message) pair for one report.

    Split out from `run` so the prompt surface can be exercised offline --
    built, measured, and diffed -- with no API key and no network. See
    docs/ai-summaries.md's smoke-test recipe.
    """
    facts = prompt.league_facts(
        latest_export("teams"),
        latest_export("roster-slots"),
        # From the report, not from the clock or the client: a scoring period
        # rolling over between the report run and this one must not put the
        # two on different seasons.
        season=report.season,
        league_id=config.LEAGUE_ID,
        team_id=config.TEAM_ID,
    )
    system = prompt.system_instruction(
        facts=facts,
        column_dictionary=_read_doc(docs_dir, DOCS["column_dictionary"]),
        report_semantics=_read_doc(docs_dir, DOCS["report_semantics"]),
        day=report.day,
    )
    user = prompt.user_message(
        report_text=report.path.read_text(),
        report_label=report.label,
        priors=[(other.label, other.path.read_text()) for other in prior_reports],
    )
    return system, user


def resolve_source(reports_dir, fallback_dir, index):
    """Pick which tree to read the reports out of, S3 first.

    **S3 is the source of truth.** `reports_dir` is the mirror pulled by
    `s3_sync.sh pull-reports`, and that is what report.yml's own last step
    writes to -- so it is the authoritative copy, and it is what the
    envelope's `report.path` keys name. The local git checkout is a
    *backup*, used only when the mirror turns up empty (a bucket wiped or
    re-created, a report.yml whose mirror step never landed).

    Strict fallback, never a merge. Merging would let a locally-rendered
    report that never reached the bucket be summarized against a
    `report.path` pointing at an object that does not exist. Either every
    report this run sees came from S3, or none did and the run says so.

    Returns (reports, directory, source) where source is "s3" or "local",
    recorded in every envelope this run writes.
    """
    from_s3 = scan_dir(reports_dir, index)
    if from_s3:
        return from_s3, reports_dir, "s3"

    if not fallback_dir:
        return [], reports_dir, "s3"

    from_local = scan_dir(fallback_dir, index)
    if not from_local:
        return [], reports_dir, "s3"

    # Loud, because this is a degraded run: the bucket should not be empty
    # while the checkout has reports, and a silent fallback would make a
    # broken mirror look like a healthy pipeline.
    print(
        f"  [warning] no reports under {reports_dir} (the S3 mirror, the source of truth) "
        f"-- falling back to the local checkout at {fallback_dir}"
    )
    print(
        "  [warning] check that report.yml's `Mirror reports/ to S3` step is landing; "
        "summaries from this run are tagged source=local"
    )
    return from_local, fallback_dir, "local"


def scan_dir(directory, index):
    return reports.scan(directory, index=index) if directory else []


def envelope(report, prior_reports, summary_markdown, system, user, usage, model,
             generated_at, source):
    """The stored artifact.

    `report.sha256` and `prompt_sha256` are the point of the envelope: they
    are what lets a later reader tell whether a summary still describes the
    report sitting next to it, and whether it was produced by the prompt
    currently in the tree. This repo's "freshness from the artifact, never
    from mtime" rule, applied to a new artifact.
    """
    report_text = report.path.read_text()
    header = reports.parse_header(report_text)
    return {
        "schema_version": SCHEMA_VERSION,
        "season": report.season,
        "week": report.week,
        "day": report.day,
        "report": {
            # Always the bucket key, even on a source=local run: that is
            # where this report belongs and will be mirrored. `source` is
            # what records where this run actually read it from.
            "path": _s3_key(report),
            "source": source,
            "title": header["title"],
            "covers": header["covers"],
            "week_window": header["week_window"],
            "rendered": header["rendered"],
            "sha256": _sha256(report_text),
        },
        "summary_markdown": summary_markdown,
        "model": model,
        "generated_at": generated_at,
        "prior_reports": [_s3_key(other) for other in prior_reports],
        "prompt_sha256": _sha256(system + "\n" + user),
        "usage": usage,
    }


def run(reports_dir, summaries_dir, out_dir, reports_fallback_dir=None, day=None, week=None,
        limit=DEFAULT_LIMIT, force=False, client=None, docs_dir=None, now=None):
    """Summarise every report that has no summary yet, newest `limit` first.

    Returns {"summarized": [...], "skipped": [...], "failed": [...]} of
    report stems. Never raises on a per-report failure -- one dead
    generation must not cost the other three summaries in the same run, and
    an unwritten envelope simply leaves that report unsummarised for the
    next trigger to pick up.
    """
    docs_dir = docs_dir or (config.PROJECT_ROOT / "docs")
    index = reports.report_index()
    all_reports, reports_dir, source = resolve_source(reports_dir, reports_fallback_dir, index)

    # Both summary trees, not just the pulled cache: see reports.has_summary.
    # A second run before the S3 push has landed must still find nothing to do.
    pending = reports.unsummarized(
        all_reports, (summaries_dir, out_dir), day=day, week=week, force=force
    )

    if not pending:
        # Told apart deliberately. "Every report already has a summary" is the
        # steady state; "no reports found" means neither S3 nor the checkout
        # had one, which is a configuration problem wearing a success
        # message. This repo's documented failure mode is a run that succeeds
        # and says nothing (docs/automation.md), so these must not print the
        # same line.
        if all_reports:
            print(
                f"  nothing to summarize -- all {len(all_reports)} report(s) "
                f"(source={source}) already have one"
            )
        else:
            print(f"  [warning] no reports found under {reports_dir} -- nothing to summarize")
        return {"summarized": [], "skipped": [], "failed": [], "source": source}

    # Newest first for the cap, then back to chronological order so a
    # multi-report run reads in the order the weeks happened.
    selected = sorted(pending, key=lambda r: r.sort_key, reverse=True)[:limit]
    skipped = [r for r in pending if r not in selected]
    selected = sorted(selected, key=lambda r: r.sort_key)

    if skipped:
        print(
            f"  [warning] --limit {limit} capped this run at {len(selected)} of "
            f"{len(pending)} unsummarized report(s); not summarized this run: "
            + ", ".join(r.stem for r in skipped)
        )

    # Constructed only once there is work: this is what makes a no-op run
    # cost nothing, and what keeps a missing GEMINI_API_KEY from raising on
    # a run that had nothing to do anyway.
    client = client or GeminiClient()
    generated_at = (now or datetime.now(ET)).isoformat(timespec="seconds")

    result = {
        "summarized": [], "skipped": [r.stem for r in skipped], "failed": [], "source": source,
    }
    for report in selected:
        prior_reports = reports.priors(all_reports, report)
        system, user = build_prompt(report, prior_reports, docs_dir)
        try:
            response = client.generate(system, user)
        except (GeminiError, requests.RequestException) as exc:
            # Warn and continue, per report. The next trigger retries this
            # one; the rest of this run still lands. RequestException is
            # caught here and not only in cmd_summarize so a single dropped
            # connection costs one summary rather than the whole batch --
            # and so cmd_summarize's "nothing was written" message stays
            # true, since by then only client construction can have failed.
            print(f"  [warning] {report.stem}: {exc} -- left unsummarized for the next run")
            result["failed"].append(report.stem)
            continue

        path = reports.summary_path(out_dir, report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                envelope(
                    report=report,
                    prior_reports=prior_reports,
                    summary_markdown=response["text"],
                    system=system,
                    user=user,
                    usage=response["usage"],
                    model=client.model,
                    generated_at=generated_at,
                    source=source,
                ),
                indent=2,
            )
            + "\n"
        )
        words = len(response["text"].split())
        print(
            f"  {report.stem}  {words} words, {len(prior_reports)} prior(s), "
            f"{response['usage'].get('totalTokenCount', 0)} tokens -> {path}"
        )
        result["summarized"].append(report.stem)

    return result
