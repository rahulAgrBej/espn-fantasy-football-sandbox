"""Orchestration: what one run summarizes, what it writes, and what it
refuses to do quietly.

Every test here runs against a stub client. Nothing in this file touches the
network or needs a credential -- `summarize.run` takes the client as an
argument precisely so it does not have to.
"""

import json

import pandas as pd
import pytest
import requests

from espn_ff.ai import reports, summarize
from espn_ff.ai.client import GeminiError

REPORT_BODY = """\
# Waiver wire and opening market -- 2026 week {week}

**Covers** week {week}'s waiver window
**Week {week}** Tue 2026-09-15 03:00 - Tue 2026-09-22 03:00 ET
**Rendered** Tue 2026-09-15 21:18 ET

## Freshness
- espn: 2026-09-15 14:10:04 ET
"""


class StubClient:
    """Records every call so a test can count generations rather than infer
    them from what landed on disk."""

    model = "stub-model"

    def __init__(self, text="A summary.", raises=None):
        self.text = text
        self.raises = raises
        self.calls = []

    def generate(self, system, user, **kwargs):
        self.calls.append((system, user))
        if self.raises is not None:
            raise self.raises
        return {
            "text": self.text,
            "usage": {"promptTokenCount": 10, "candidatesTokenCount": 2, "totalTokenCount": 12},
        }


@pytest.fixture(autouse=True)
def no_exports_on_disk(monkeypatch):
    """data/ is gitignored but may exist locally. Pin the prompt's league
    facts to "unavailable" so these tests read the same on a laptop and on a
    cold runner."""
    monkeypatch.setattr(summarize, "latest_export", lambda name: pd.DataFrame())


def write_report(root, season, week, rendered_on, stem_tail):
    path = root / str(season) / f"week-{week:02d}" / f"{rendered_on}-{stem_tail}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(REPORT_BODY.format(week=week))
    return path


def dirs(tmp_path):
    return {
        "reports_dir": tmp_path / "cache-reports",
        "summaries_dir": tmp_path / "cache-summaries",
        "out_dir": tmp_path / "summaries",
        "docs_dir": tmp_path / "docs",
    }


# --- what a run does at all ----------------------------------------------


def test_a_run_with_nothing_pending_spends_no_generation(tmp_path):
    """The idempotency guarantee, from the orchestration side: the event
    trigger and the AWS backstop both land here minutes apart, and the
    second must cost nothing."""
    paths = dirs(tmp_path)
    write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")
    client = StubClient()

    first = summarize.run(**paths, client=client)
    second = summarize.run(**paths, client=client)

    assert first["summarized"] == ["2026-09-15-tuesday-waiver-wire"]
    assert second == {"summarized": [], "skipped": [], "failed": [], "source": "s3"}
    assert len(client.calls) == 1, "the second run generated again"


def test_the_existing_summary_check_reads_the_cache_not_the_output_tree(tmp_path):
    """The two directories are not interchangeable. `summaries_dir` is the
    pulled history; `out_dir` holds one run. A run whose output tree is
    empty but whose cache has the summary must still find nothing to do --
    that is exactly the shape of every scheduled run after the first."""
    paths = dirs(tmp_path)
    write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")
    report = reports.scan(paths["reports_dir"])[0]

    cached = reports.summary_path(paths["summaries_dir"], report)
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text("{}\n")

    client = StubClient()
    result = summarize.run(**paths, client=client)

    assert result["summarized"] == []
    assert client.calls == []
    assert not paths["out_dir"].exists()


def test_new_envelopes_land_in_the_output_tree_not_the_cache(tmp_path):
    """Writing into the pulled cache would make the append-only push either
    wrong (it would re-upload unchanged history) or destructive (if it ever
    gained --delete)."""
    paths = dirs(tmp_path)
    write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")

    summarize.run(**paths, client=StubClient())

    assert (paths["out_dir"] / "2026" / "week-02" / "2026-09-15-tuesday-waiver-wire.json").exists()
    assert not paths["summaries_dir"].exists()


# --- the cap --------------------------------------------------------------


def test_the_limit_takes_the_newest_and_names_what_it_dropped(tmp_path, capsys):
    """A first run over a backfilled season would otherwise fire one call
    per report with no warning. Silence is this repo's documented failure
    mode, so the cap has to say what it left."""
    paths = dirs(tmp_path)
    for week, day_of_month in enumerate(range(8, 14), start=1):
        write_report(paths["reports_dir"], 2026, week, f"2026-09-{day_of_month:02d}", "tuesday-waiver-wire")

    result = summarize.run(**paths, client=StubClient(), limit=2)
    printed = capsys.readouterr().out

    assert result["summarized"] == [
        "2026-09-12-tuesday-waiver-wire",
        "2026-09-13-tuesday-waiver-wire",
    ]
    assert len(result["skipped"]) == 4
    assert "--limit 2 capped this run" in printed
    for dropped in result["skipped"]:
        assert dropped in printed, "the cap dropped a report without naming it"


def test_the_cap_says_nothing_when_it_did_not_fire(tmp_path, capsys):
    paths = dirs(tmp_path)
    write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")

    summarize.run(**paths, client=StubClient(), limit=4)

    assert "capped this run" not in capsys.readouterr().out


# --- failure is per report, never per run --------------------------------


def test_one_dead_generation_does_not_cost_the_others(tmp_path, capsys):
    """Warn and continue. A summary is additive, so a model that dies on the
    third report must still leave the first two on disk."""
    paths = dirs(tmp_path)
    write_report(paths["reports_dir"], 2026, 1, "2026-09-08", "tuesday-waiver-wire")
    write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")

    class FlakyClient(StubClient):
        def generate(self, system, user, **kwargs):
            self.calls.append((system, user))
            if len(self.calls) == 1:
                raise GeminiError("HTTP 503 after 4 attempts")
            return {"text": "ok", "usage": {"totalTokenCount": 1}}

    result = summarize.run(**paths, client=FlakyClient())

    assert result["failed"] == ["2026-09-08-tuesday-waiver-wire"]
    assert result["summarized"] == ["2026-09-15-tuesday-waiver-wire"]
    assert "left unsummarized for the next run" in capsys.readouterr().out


def test_a_failed_report_writes_no_envelope_so_the_next_run_retries_it(tmp_path):
    """A partial or placeholder envelope would make the report look done and
    silently end the self-healing property."""
    paths = dirs(tmp_path)
    write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")

    summarize.run(**paths, client=StubClient(raises=GeminiError("dead")))
    assert list(paths["out_dir"].rglob("*.json")) == [] if paths["out_dir"].exists() else True

    result = summarize.run(**paths, client=StubClient())
    assert result["summarized"] == ["2026-09-15-tuesday-waiver-wire"]


def test_a_missing_doc_degrades_the_prompt_rather_than_failing_the_run(tmp_path, capsys):
    paths = dirs(tmp_path)
    write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")
    client = StubClient()

    result = summarize.run(**paths, client=client)

    assert result["summarized"] == ["2026-09-15-tuesday-waiver-wire"]
    assert "docs/data-sources.md not found" in capsys.readouterr().out
    assert "(docs/data-sources.md was not available to this run.)" in client.calls[0][0]


# --- the envelope ---------------------------------------------------------


@pytest.fixture
def envelope(tmp_path):
    paths = dirs(tmp_path)
    write_report(paths["reports_dir"], 2026, 1, "2026-09-08", "tuesday-waiver-wire")
    write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")
    summarize.run(**paths, client=StubClient("Claim Gibbs."))
    written = sorted(paths["out_dir"].rglob("*.json"))
    return json.loads(written[-1].read_text())


def test_the_envelope_carries_its_own_provenance(envelope):
    assert envelope["schema_version"] == summarize.SCHEMA_VERSION
    assert envelope["season"] == 2026
    assert envelope["week"] == 2
    assert envelope["day"] == "tuesday-waivers"
    assert envelope["summary_markdown"] == "Claim Gibbs."
    assert envelope["model"] == "stub-model"
    assert envelope["usage"]["totalTokenCount"] == 12


def test_the_envelope_names_the_bucket_key_not_a_runner_cache_path(envelope):
    """A path under .cache/s3-reports is meaningless once the runner is
    gone. The durable location is the one worth recording."""
    assert envelope["report"]["path"] == "reports/2026/week-02/2026-09-15-tuesday-waiver-wire.md"
    assert envelope["prior_reports"] == ["reports/2026/week-01/2026-09-08-tuesday-waiver-wire.md"]


def test_the_envelope_copies_the_header_block_it_parsed(envelope):
    """The four header fields come from parsing the artifact, not from
    re-deriving them -- so they describe the report on disk even if a later
    render would produce something different."""
    assert envelope["report"]["title"] == "Waiver wire and opening market -- 2026 week 2"
    assert envelope["report"]["covers"] == "week 2's waiver window"
    assert envelope["report"]["rendered"] == "Tue 2026-09-15 21:18 ET"
    assert envelope["report"]["week_window"].endswith("ET")


def test_the_two_hashes_are_present_and_distinct(envelope):
    """`report.sha256` and `prompt_sha256` are what let a later reader tell
    whether a summary still describes the report beside it, and whether the
    prompt that produced it is still the one in the tree."""
    assert len(envelope["report"]["sha256"]) == 64
    assert len(envelope["prompt_sha256"]) == 64
    assert envelope["report"]["sha256"] != envelope["prompt_sha256"]


def test_the_generated_at_stamp_is_et_with_an_offset(envelope):
    """Naive UTC under an ET dateline is the exact trap render._fmt_ts
    exists to avoid; the envelope must not reintroduce it."""
    assert envelope["generated_at"][-6] in "+-"
    assert "T" in envelope["generated_at"]


def test_the_report_hash_changes_when_the_report_does(tmp_path):
    paths = dirs(tmp_path)
    report_path = write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")

    summarize.run(**paths, client=StubClient())
    first = json.loads(next(paths["out_dir"].rglob("*.json")).read_text())["report"]["sha256"]

    report_path.write_text(REPORT_BODY.format(week=2) + "\nan extra line\n")
    summarize.run(**paths, client=StubClient(), force=True)
    second = json.loads(next(paths["out_dir"].rglob("*.json")).read_text())["report"]["sha256"]

    assert first != second


def test_an_empty_report_cache_warns_instead_of_reporting_success(tmp_path, capsys):
    """"Every report already has a summary" and "pull-reports produced
    nothing" are not the same event. The second is a configuration problem
    wearing a success message, which is exactly the failure mode
    docs/automation.md already lists for reports."""
    paths = dirs(tmp_path)

    summarize.run(**paths, client=StubClient())

    assert "[warning] no reports found" in capsys.readouterr().out


def test_the_steady_state_does_not_warn(tmp_path, capsys):
    paths = dirs(tmp_path)
    write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")
    summarize.run(**paths, client=StubClient())
    capsys.readouterr()

    summarize.run(**paths, client=StubClient())
    printed = capsys.readouterr().out

    assert "all 1 report(s) (source=s3) already have one" in printed
    assert "[warning]" not in printed


def test_a_dropped_connection_costs_one_summary_not_the_batch(tmp_path, capsys):
    """requests.RequestException is caught per report, not only at the CLI
    boundary. Catching it only there would let one blip on the first report
    abort the other three -- and would make cmd_summarize's "nothing was
    written" message a lie for the ones already on disk."""
    paths = dirs(tmp_path)
    write_report(paths["reports_dir"], 2026, 1, "2026-09-08", "tuesday-waiver-wire")
    write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")

    class DroppingClient(StubClient):
        def generate(self, system, user, **kwargs):
            self.calls.append((system, user))
            if len(self.calls) == 1:
                raise requests.ConnectionError("connection reset by peer")
            return {"text": "ok", "usage": {"totalTokenCount": 1}}

    result = summarize.run(**paths, client=DroppingClient())

    assert result["failed"] == ["2026-09-08-tuesday-waiver-wire"]
    assert result["summarized"] == ["2026-09-15-tuesday-waiver-wire"]
    assert "connection reset by peer" in capsys.readouterr().out


# --- S3 is the source of truth, the checkout is the backup ----------------


def test_s3_wins_when_both_trees_have_reports(tmp_path, capsys):
    """Not a merge and not newest-wins: the S3 mirror is authoritative, full
    stop. The envelope's `report.path` is a bucket key, so a report only the
    checkout has would be summarized against an object that does not exist."""
    paths = dirs(tmp_path)
    fallback = tmp_path / "checkout-reports"
    write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")
    write_report(fallback, 2026, 3, "2026-09-22", "tuesday-waiver-wire")

    result = summarize.run(**paths, reports_fallback_dir=fallback, client=StubClient())
    printed = capsys.readouterr().out

    assert result["source"] == "s3"
    assert result["summarized"] == ["2026-09-15-tuesday-waiver-wire"]
    assert "falling back" not in printed
    envelope = json.loads(next(paths["out_dir"].rglob("*.json")).read_text())
    assert envelope["report"]["source"] == "s3"


def test_the_checkout_is_used_when_the_s3_mirror_is_empty(tmp_path, capsys):
    paths = dirs(tmp_path)
    fallback = tmp_path / "checkout-reports"
    write_report(fallback, 2026, 2, "2026-09-15", "tuesday-waiver-wire")

    result = summarize.run(**paths, reports_fallback_dir=fallback, client=StubClient())

    assert result["source"] == "local"
    assert result["summarized"] == ["2026-09-15-tuesday-waiver-wire"]
    envelope = json.loads(next(paths["out_dir"].rglob("*.json")).read_text())
    assert envelope["report"]["source"] == "local"
    # Still the bucket key: that is where the report belongs and will be
    # mirrored. `source` is what records where this run read it from.
    assert envelope["report"]["path"].startswith("reports/2026/week-02/")


def test_falling_back_to_the_checkout_is_loud(tmp_path, capsys):
    """A bucket that is empty while the checkout has reports means the mirror
    step is broken. A silent fallback would make that look like a healthy
    pipeline for as long as nobody checked."""
    paths = dirs(tmp_path)
    fallback = tmp_path / "checkout-reports"
    write_report(fallback, 2026, 2, "2026-09-15", "tuesday-waiver-wire")

    summarize.run(**paths, reports_fallback_dir=fallback, client=StubClient())
    printed = capsys.readouterr().out

    assert "[warning]" in printed
    assert "the source of truth" in printed
    assert "Mirror reports/ to S3" in printed
    assert "source=local" in printed


def test_both_trees_empty_reports_against_the_s3_path(tmp_path, capsys):
    """The message must name the bucket mirror, not the fallback -- that is
    the one a person needs to go look at."""
    paths = dirs(tmp_path)

    result = summarize.run(**paths, reports_fallback_dir=tmp_path / "empty", client=StubClient())

    assert result["source"] == "s3"
    assert str(paths["reports_dir"]) in capsys.readouterr().out


def test_no_fallback_configured_still_works(tmp_path):
    """`reports_fallback_dir` is optional -- a caller that wants S3 or
    nothing gets exactly that."""
    paths = dirs(tmp_path)
    write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")

    result = summarize.run(**paths, client=StubClient())

    assert result["source"] == "s3"
    assert result["summarized"] == ["2026-09-15-tuesday-waiver-wire"]
