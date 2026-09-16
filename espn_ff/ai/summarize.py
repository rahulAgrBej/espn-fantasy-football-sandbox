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
from . import news, prompt, reports
from .client import (
    GOOGLE_SEARCH,
    NEWS_MAX_OUTPUT_TOKENS,
    USAGE_FIELDS,
    GeminiClient,
    GeminiError,
)

# 2 adds the `news` and `news_error` keys. Strictly additive -- every v1 key
# keeps its name, position and meaning, which is what lets the news-only
# backfill path copy them off an existing envelope verbatim.
SCHEMA_VERSION = 2

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


def build_news(rosters_df, report, today):
    """The `(group, system, user, group_df)` tuples for one report's news
    calls, plus `{group: reason}` for the groups with nobody in them.

    Split out from `generate_news` for the same reason `build_prompt` is
    split out of `run`: the whole news prompt surface can then be built,
    measured and diffed offline with no API key and no network. See
    docs/ai-summaries.md's verification recipe.

    A group with no players is **skipped, not prompted**. An empty IR is the
    normal case for most of a season, and spending a billed grounded call to
    be told nobody is on IR is waste the reader would have to pay for.
    """
    groups = news.roster_groups(rosters_df, week=report.week, team_id=config.TEAM_ID)
    prompts, skipped = [], {}
    for group in news.GROUPS:
        group_df = groups[group]
        if group_df.empty:
            skipped[group] = f"no players in the {group} group for week {report.week}"
            continue
        prompts.append(
            (
                group,
                news.system_instruction(group, report.season, report.week, today),
                news.user_message(group_df, report.season, report.week, today),
                group_df,
            )
        )
    return prompts, skipped


def generate_news(client, prompts, skipped, report, generated_at, roster_export):
    """Run one report's grounded calls and assemble the `news` block.

    **All-or-nothing per report.** The first group to fail raises, and `run`
    stores `news: null` with the reason rather than a partial block. Partial
    news is worse than none: a reader cannot tell "no news for this player"
    from "the call covering him died", and this repo refuses that class of
    silent hole everywhere else.

    Every grounded call's `webSearchQueries` is recorded and summed into
    `search_query_count`, because that is the **billable** unit -- the vendor
    bills per query the model chose to execute, not per request -- and it is
    the only thing that will turn docs/ai-summaries.md's monthly projection
    from Inferred into Observed.
    """
    players, groups = [], {}
    totals = {field: 0 for field in USAGE_FIELDS}
    query_count = 0
    prompt_parts = []

    for group, system, user, group_df in prompts:
        prompt_parts.append(system + "\n" + user)
        response = client.generate(
            system,
            user,
            max_output_tokens=NEWS_MAX_OUTPUT_TOKENS,
            tools=GOOGLE_SEARCH,
            response_format=news.response_format(),
        )

        group_players, warnings = news.parse_players(response["text"], group_df, group)
        for warning in warnings:
            print(f"  [warning] {report.stem}: {warning}")
        players.extend(group_players)

        grounding = response.get("grounding")
        if grounding is None:
            # The tool was requested and the response carries no grounding
            # metadata, which means no search ran -- so whatever the model
            # returned came from recall, in direct violation of rule 1.
            # Loud, because the output still looks like news.
            print(
                f"  [warning] {report.stem}: the {group} call returned no grounding metadata "
                "-- the search tool did not fire, so its claims are ungrounded"
            )
            grounding = {"sources": [], "search_queries": [], "search_entry_point": ""}

        query_count += len(grounding["search_queries"])
        for field in USAGE_FIELDS:
            totals[field] += response["usage"].get(field, 0)

        groups[group] = {
            "players": len(group_df),
            "search_queries": grounding["search_queries"],
            "sources": grounding["sources"],
            "search_entry_point": grounding["search_entry_point"],
            "usage": response["usage"],
        }

    for group, reason in skipped.items():
        groups[group] = {"skipped": reason}

    return {
        "model": client.model,
        "grounded": True,
        "generated_at": generated_at,
        "roster_week": report.week,
        "roster_export": roster_export,
        "prompt_sha256": _sha256("\n".join(prompt_parts)),
        "search_query_count": query_count,
        "players": players,
        "groups": groups,
        "usage": totals,
    }


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
             generated_at, source, news_block=None, news_error=None):
    """The stored artifact.

    `report.sha256` and `prompt_sha256` are the point of the envelope: they
    are what lets a later reader tell whether a summary still describes the
    report sitting next to it, and whether it was produced by the prompt
    currently in the tree. This repo's "freshness from the artifact, never
    from mtime" rule, applied to a new artifact.

    `news_block` and `news_error` are mutually exclusive by construction:
    exactly one is populated. Keeping the error in a **sibling** key rather
    than inside `news` is what makes the completeness test trivially
    `news is not None` -- an error object living under `news` would make a
    failed report look summarized to the next run's three-state check.
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
        "news": news_block,
        "news_error": news_error,
    }


def with_news(existing, news_block=None, news_error=None):
    """An existing envelope with only its news layer replaced.

    The backfill path. Every v1 key -- `summary_markdown`, `model`,
    `generated_at`, `prompt_sha256`, `usage`, `prior_reports` and the whole
    `report` block -- is carried across **verbatim**, so a run that
    regenerates news does not restate, re-derive or re-bill the summary. In
    particular `generated_at` keeps naming when the summary was written,
    which is the provenance a reader needs; the news carries its own.

    `schema_version` is stamped forward, since the result is a v2 object
    whatever the file on disk said.
    """
    return {
        **existing,
        "schema_version": SCHEMA_VERSION,
        "news": news_block,
        "news_error": news_error,
    }


def run(reports_dir, summaries_dir, out_dir, reports_fallback_dir=None, day=None, week=None,
        limit=DEFAULT_LIMIT, force=False, client=None, docs_dir=None, now=None,
        with_news_layer=True):
    """Summarise and research every report that still owes either, newest
    `limit` first.

    Returns {"summarized": [...], "skipped": [...], "failed": [...]} of
    report stems. Never raises on a per-report failure -- one dead
    generation must not cost the other three reports in the same run, and an
    unwritten envelope simply leaves that report for the next trigger.

    Three states per report rather than two (see reports.envelope_needs):

      no envelope             generate the summary and the news   1 + 3 calls
      summary but no news     regenerate the news only            3 calls
      both                    skip                                0 calls

    The middle row is what makes a failed grounded call cheap to recover
    from: the summary is already on disk, and re-running it to reach the
    news would re-bill ~27k prompt tokens for an answer we already have.

    `with_news_layer=False` (the --no-news path) collapses this back to the
    original two states, so the flag cannot make a run rewrite envelopes it
    has nothing new to put in.
    """
    docs_dir = docs_dir or (config.PROJECT_ROOT / "docs")
    index = reports.report_index()
    all_reports, reports_dir, source = resolve_source(reports_dir, reports_fallback_dir, index)

    # Both summary trees, not just the pulled cache: see
    # reports.existing_envelope. A second run before the S3 push has landed
    # must still find nothing to do.
    pending = reports.pending_work(
        all_reports, (summaries_dir, out_dir), day=day, week=week, force=force,
        want_news=with_news_layer,
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
    selected = sorted(pending, key=lambda item: item[0].sort_key, reverse=True)[:limit]
    skipped = [item for item in pending if item not in selected]
    selected = sorted(selected, key=lambda item: item[0].sort_key)

    if skipped:
        print(
            f"  [warning] --limit {limit} capped this run at {len(selected)} of "
            f"{len(pending)} pending report(s); not processed this run: "
            + ", ".join(report.stem for report, _, _ in skipped)
        )

    # Constructed only once there is work: this is what makes a no-op run
    # cost nothing, and what keeps a missing GEMINI_API_KEY from raising on
    # a run that had nothing to do anyway.
    client = client or GeminiClient()
    # Read the clock once. Two reads either side of midnight would date the
    # news prompt to a different day than the envelope claims it was written.
    started = now or datetime.now(ET)
    generated_at = started.isoformat(timespec="seconds")
    today = started.date()

    # Loaded once for the whole run, not per report: every report in a run
    # is normally the same week, and `latest_export` re-reads the CSV each
    # call. Empty is not fatal -- it becomes an explicit per-report
    # news_error, the same degrade-with-a-reason discipline the prompt's
    # league-facts section uses.
    rosters_df = latest_export("weekly-rosters") if with_news_layer else None
    roster_export = _roster_export_name() if with_news_layer else None

    result = {
        "summarized": [], "skipped": [report.stem for report, _, _ in skipped],
        "failed": [], "source": source,
    }
    for report, existing, mode in selected:
        if mode == "news" and _report_moved(report, existing):
            # The report was re-rendered after this summary was written, so
            # the summary describes a document that no longer exists.
            # Bolting fresh news onto it would produce an envelope whose two
            # halves disagree about which report they are about. Observed
            # 2026-09-16: a Wednesday summary written from a pre-refresh
            # render survived the corrected re-render, which is the failure
            # summary.yml's `force` input was added for.
            print(
                f"  [warning] {report.stem}: the report changed since its summary was "
                "written -- regenerating both rather than backfilling news onto a stale summary"
            )
            mode = "full"

        prior_reports = reports.priors(all_reports, report)

        if mode == "full":
            system, user = build_prompt(report, prior_reports, docs_dir)
            try:
                response = client.generate(system, user)
            except (GeminiError, requests.RequestException) as exc:
                # Warn and continue, per report. The next trigger retries
                # this one; the rest of this run still lands.
                # RequestException is caught here and not only in
                # cmd_summarize so a single dropped connection costs one
                # summary rather than the whole batch -- and so
                # cmd_summarize's "nothing was written" message stays true,
                # since by then only client construction can have failed.
                print(f"  [warning] {report.stem}: {exc} -- left unsummarized for the next run")
                result["failed"].append(report.stem)
                continue
        # else: the backfill. No summary call at all -- that is the entire
        # point of the three-state check, and `with_news` below carries the
        # existing summary and its provenance across untouched.

        news_block, news_error = None, None
        if with_news_layer:
            news_block, news_error = _news_for(
                client, rosters_df, report, generated_at, today, roster_export
            )
        elif mode == "news":
            # Unreachable: want_news=False never yields mode "news".
            raise AssertionError("--no-news produced a news-only backfill")

        if mode == "full":
            record = envelope(
                report=report,
                prior_reports=prior_reports,
                summary_markdown=response["text"],
                system=system,
                user=user,
                usage=response["usage"],
                model=client.model,
                generated_at=generated_at,
                source=source,
                news_block=news_block,
                news_error=news_error,
            )
        else:
            record = with_news(existing, news_block=news_block, news_error=news_error)

        path = reports.summary_path(out_dir, report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2) + "\n")
        _log_report(report, record, mode, path)
        result["summarized"].append(report.stem)

    return result


def _report_moved(report, existing):
    """True when the report on disk no longer hashes to what its stored
    envelope says it summarised."""
    stored = ((existing or {}).get("report") or {}).get("sha256")
    return bool(stored) and stored != _sha256(report.path.read_text())


def _news_for(client, rosters_df, report, generated_at, today, roster_export):
    """`(news_block, news_error)` -- exactly one populated.

    Every failure here is caught and turned into a stored reason rather than
    a raise. The summary beside it is already generated (or already on
    disk), and losing it to a dead grounded call would be the tail wagging
    the dog. `reports.envelope_needs` sees `news is None` on the next run
    and regenerates only this part.
    """
    try:
        prompts, skipped = build_news(rosters_df, report, today)
    except (KeyError, ValueError) as exc:
        return None, f"could not build the news prompts: {exc}"

    if not prompts:
        # Every group empty means the roster export is missing or holds no
        # rows for this team and week -- a restore-out problem, not a model
        # problem, and worth saying so in the stored artifact.
        return None, (
            f"no weekly-rosters rows for team {config.TEAM_ID} in week {report.week} "
            "-- check that restore-out fetched the weekly-rosters export"
        )

    try:
        block = generate_news(client, prompts, skipped, report, generated_at, roster_export)
    except (GeminiError, requests.RequestException, news.NewsFormatError) as exc:
        print(f"  [warning] {report.stem}: news unavailable -- {exc}")
        return None, str(exc)
    return block, None


def _roster_export_name():
    """The filename `latest_export` would have picked, recorded in the
    envelope so a reader knows which roster snapshot the news describes.
    None when no export exists -- the caller turns that into a news_error."""
    candidates = sorted(config.OUT_DIR.glob("*-weekly-rosters.csv"))
    return candidates[-1].name if candidates else None


def _log_report(report, record, mode, path):
    """One line per report, naming the grounded query count.

    The query count is in the run output and not only in the stored
    envelope, because it is the billable figure and the person who notices a
    runaway is reading workflow logs, not reading S3.
    """
    words = len(record["summary_markdown"].split())
    news_block = record.get("news")
    if news_block:
        news_note = (
            f"news {len(news_block['players'])} players, "
            f"{news_block['search_query_count']} searches"
        )
    elif record.get("news_error"):
        news_note = "news FAILED (retried next run)"
    else:
        news_note = "news skipped"
    prefix = "backfilled news for " if mode == "news" else ""
    print(f"  {prefix}{report.stem}  {words} words, {news_note} -> {path}")
