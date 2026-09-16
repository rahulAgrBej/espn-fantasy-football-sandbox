"""Orchestration: what one run summarizes, what it writes, and what it
refuses to do quietly.

Every test here runs against a stub client. Nothing in this file touches the
network or needs a credential -- `summarize.run` takes the client as an
argument precisely so it does not have to.
"""

import json
import re

import pandas as pd
import pytest
import requests

from espn_ff import config
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

# The player_id column of `news.user_message`'s table: right-aligned digits
# followed by the two-space gutter. Pulling the ids back out of the real
# prompt rather than hard-coding them means the stub can only answer for
# players the prompt actually carried.
PROMPTED_IDS = re.compile(r"^\s*(\d+)\s{2,}", re.MULTILINE)


def roster_frame(weeks=(1, 2)):
    """One starter, one bench player and one on IR for our team, plus a
    rival's player that every group filter must exclude."""
    rows = []
    for week in weeks:
        rows += [
            {"season": 2026, "week": week, "team_id": config.TEAM_ID, "player_id": 11,
             "player_name": "Starter One", "position": "QB", "pro_team": "CHI",
             "lineup_slot": "QB", "started": True, "injury_status": "ACTIVE"},
            {"season": 2026, "week": week, "team_id": config.TEAM_ID, "player_id": 22,
             "player_name": "Bench Two", "position": "WR", "pro_team": "DAL",
             "lineup_slot": "Bench", "started": False, "injury_status": "ACTIVE"},
            {"season": 2026, "week": week, "team_id": config.TEAM_ID, "player_id": 33,
             "player_name": "Injured Three", "position": "RB", "pro_team": "NYG",
             "lineup_slot": "IR", "started": False, "injury_status": "OUT"},
            {"season": 2026, "week": week, "team_id": config.TEAM_ID + 1, "player_id": 44,
             "player_name": "Rival Four", "position": "TE", "pro_team": "BAL",
             "lineup_slot": "TE", "started": True, "injury_status": "ACTIVE"},
        ]
    return pd.DataFrame(rows)


class StubClient:
    """Records every call so a test can count generations rather than infer
    them from what landed on disk.

    Serves both layers off one method, the way the real client does: a call
    carrying `tools` is a grounded news call and answers with the news
    schema, anything else is the summary call. `grounded_calls` counts the
    former, which is the billed one.
    """

    model = "stub-model"

    def __init__(self, text="A summary.", raises=None, news_raises=None, queries=2):
        self.text = text
        self.raises = raises
        self.news_raises = news_raises
        self.queries = queries
        self.calls = []
        self.grounded_calls = []

    def generate(self, system, user, tools=None, response_format=None, **kwargs):
        if tools:
            self.grounded_calls.append((system, user, tools, response_format))
            if self.news_raises is not None:
                raise self.news_raises
            return self._news(user)

        self.calls.append((system, user))
        if self.raises is not None:
            raise self.raises
        return {
            "text": self.text,
            "usage": {"promptTokenCount": 10, "candidatesTokenCount": 2, "totalTokenCount": 12},
            "grounding": None,
        }

    def _news(self, user):
        players = [
            {"player_id": int(pid), "found": True, "headline": "A headline.",
             "detail": "Some detail.", "as_of": "2026-09-16"}
            for pid in PROMPTED_IDS.findall(user)
        ]
        return {
            "text": json.dumps({"players": players}),
            "usage": {"promptTokenCount": 5, "candidatesTokenCount": 3, "totalTokenCount": 8},
            "grounding": {
                "sources": [{"uri": "https://example.test/a", "title": "A"}],
                "search_queries": [f"query {n}" for n in range(self.queries)],
                "search_entry_point": "<div>suggestions</div>",
            },
        }


@pytest.fixture(autouse=True)
def exports_on_disk(monkeypatch):
    """data/ is gitignored but may exist locally. Pin both exports these
    tests read so they behave the same on a laptop and on a cold runner:
    the prompt's league facts degrade to "unavailable", and the news layer
    gets a small, known roster."""
    monkeypatch.setattr(
        summarize, "latest_export",
        lambda name: roster_frame() if name == "weekly-rosters" else pd.DataFrame(),
    )
    monkeypatch.setattr(summarize, "_roster_export_name", lambda: "01-01-2026-weekly-rosters.csv")


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
    # A *complete* envelope: both layers present. A file that merely exists
    # is no longer enough -- since v2 an envelope can owe news, and the
    # three-state check has to see that this one does not.
    cached.write_text(json.dumps({"summary_markdown": "done", "news": {"players": []}}) + "\n")

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


# --- the news layer -------------------------------------------------------


def envelope_at(paths, stem="2026-09-15-tuesday-waiver-wire", week=2):
    return json.loads((paths["out_dir"] / "2026" / f"week-{week:02d}" / f"{stem}.json").read_text())


def test_the_envelope_carries_both_layers_and_is_version_2(tmp_path):
    paths = dirs(tmp_path)
    write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")

    summarize.run(**paths, client=StubClient())
    record = envelope_at(paths)

    assert record["schema_version"] == 2
    assert record["summary_markdown"] == "A summary."
    assert record["news_error"] is None
    assert record["news"]["grounded"] is True
    assert record["news"]["roster_week"] == 2
    assert record["news"]["roster_export"] == "01-01-2026-weekly-rosters.csv"


def test_the_news_block_covers_every_rostered_player_exactly_once(tmp_path):
    """The guarantee the structured shape exists for. One starter, one bench
    player and one on IR go in; three entries come out, and the rival's
    player does not."""
    paths = dirs(tmp_path)
    write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")

    summarize.run(**paths, client=StubClient())
    players = envelope_at(paths)["news"]["players"]

    assert [p["player_id"] for p in players] == [11, 22, 33]
    assert {p["group"] for p in players} == {"starters", "bench", "ir"}
    assert all(p["found"] for p in players)


def test_one_grounded_call_per_non_empty_group(tmp_path):
    paths = dirs(tmp_path)
    write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")

    client = StubClient()
    summarize.run(**paths, client=client)

    assert len(client.calls) == 1, "more than one summary call"
    assert len(client.grounded_calls) == 3, "expected one grounded call per group"


def test_an_empty_group_is_recorded_as_skipped_and_costs_no_call(tmp_path, monkeypatch):
    """An empty IR is the normal case for most of a season. Paying a billed
    grounded call to be told so is waste the reader funds."""
    paths = dirs(tmp_path)
    write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")
    no_ir = roster_frame()[lambda df: df["lineup_slot"] != "IR"]
    monkeypatch.setattr(
        summarize, "latest_export",
        lambda name: no_ir if name == "weekly-rosters" else pd.DataFrame(),
    )

    client = StubClient()
    summarize.run(**paths, client=client)

    assert len(client.grounded_calls) == 2
    assert "no players in the ir group" in envelope_at(paths)["news"]["groups"]["ir"]["skipped"]


def test_the_billed_query_count_is_summed_across_groups(tmp_path, capsys):
    """The one number that turns the monthly projection in
    docs/ai-summaries.md from Inferred into Observed -- and the vendor bills
    per query, not per request, so it is not derivable from the call count."""
    paths = dirs(tmp_path)
    write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")

    summarize.run(**paths, client=StubClient(queries=4))
    record = envelope_at(paths)

    assert record["news"]["search_query_count"] == 12, "3 groups x 4 queries"
    assert sum(len(g.get("search_queries", [])) for g in record["news"]["groups"].values()) == 12
    assert "12 searches" in capsys.readouterr().out, "the meter is invisible in the run log"


def test_the_news_usage_sums_the_groups(tmp_path):
    paths = dirs(tmp_path)
    write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")

    summarize.run(**paths, client=StubClient())

    assert envelope_at(paths)["news"]["usage"]["totalTokenCount"] == 24, "3 groups x 8"


def test_a_grounded_call_that_returned_no_metadata_is_called_out(tmp_path, capsys):
    """No grounding metadata means the search never fired, so whatever came
    back is recall -- rule 1 violated, with output that looks identical to a
    real finding."""
    paths = dirs(tmp_path)
    write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")

    class UngroundedClient(StubClient):
        def _news(self, user):
            return {**super()._news(user), "grounding": None}

    summarize.run(**paths, client=UngroundedClient())

    assert "the search tool did not fire" in capsys.readouterr().out


# --- news fails, the summary still lands ---------------------------------


def test_a_dead_news_call_still_writes_the_summary(tmp_path, capsys):
    """A summary is additive and a news block is additive to that. Losing
    the summary to a failed grounded call would be the tail wagging the
    dog."""
    paths = dirs(tmp_path)
    write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")

    result = summarize.run(**paths, client=StubClient(news_raises=GeminiError("HTTP 503")))
    record = envelope_at(paths)

    assert result["summarized"] == ["2026-09-15-tuesday-waiver-wire"]
    assert record["summary_markdown"] == "A summary."
    assert record["news"] is None
    assert "HTTP 503" in record["news_error"]
    assert "news unavailable" in capsys.readouterr().out


def test_a_missing_roster_export_names_the_step_that_should_have_restored_it(tmp_path, monkeypatch):
    """A restore-out problem, not a model problem. Saying so in the stored
    artifact is what stops the next reader debugging the prompt."""
    paths = dirs(tmp_path)
    write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")
    monkeypatch.setattr(summarize, "latest_export", lambda name: pd.DataFrame())

    client = StubClient()
    summarize.run(**paths, client=client)

    assert client.grounded_calls == [], "prompted with no roster"
    assert "restore-out" in envelope_at(paths)["news_error"]


def test_an_unreadable_news_answer_leaves_news_null_rather_than_partial(tmp_path):
    paths = dirs(tmp_path)
    write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")

    class GarbageClient(StubClient):
        def _news(self, user):
            return {"text": "sorry, I cannot help with that", "usage": {}, "grounding": None}

    summarize.run(**paths, client=GarbageClient())
    record = envelope_at(paths)

    assert record["news"] is None
    assert "not valid JSON" in record["news_error"]


# --- the backfill ---------------------------------------------------------


def test_a_report_owing_only_news_regenerates_only_the_news(tmp_path):
    """The point of the three-state check. Re-running the summary to reach
    the news would re-bill ~27k prompt tokens for an answer already on
    disk."""
    paths = dirs(tmp_path)
    write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")
    summarize.run(**paths, client=StubClient(news_raises=GeminiError("dead")))
    before = envelope_at(paths)

    client = StubClient()
    result = summarize.run(**paths, client=client)
    after = envelope_at(paths)

    assert result["summarized"] == ["2026-09-15-tuesday-waiver-wire"]
    assert client.calls == [], "the backfill regenerated the summary"
    assert len(client.grounded_calls) == 3
    assert after["news"] is not None and after["news_error"] is None
    # Every v1 key carried across verbatim: the summary's provenance still
    # describes when the summary was written, not when the news was.
    for key in ("summary_markdown", "generated_at", "prompt_sha256", "usage", "report"):
        assert after[key] == before[key], f"the backfill rewrote {key}"


def test_the_backfill_logs_itself_as_a_backfill(tmp_path, capsys):
    paths = dirs(tmp_path)
    write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")
    summarize.run(**paths, client=StubClient(news_raises=GeminiError("dead")))
    capsys.readouterr()

    summarize.run(**paths, client=StubClient())

    assert "backfilled news for" in capsys.readouterr().out


def test_a_re_rendered_report_regenerates_both_rather_than_backfilling(tmp_path, capsys):
    """Observed 2026-09-16: a Wednesday summary written from a pre-refresh
    render survived the corrected re-render. Bolting fresh news onto it
    would produce an envelope whose two halves describe different
    documents."""
    paths = dirs(tmp_path)
    path = write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")
    summarize.run(**paths, client=StubClient(news_raises=GeminiError("dead")))

    path.write_text(path.read_text() + "\n## A corrected section\n")

    client = StubClient(text="A corrected summary.")
    summarize.run(**paths, client=client)

    assert len(client.calls) == 1, "the stale summary was kept"
    assert envelope_at(paths)["summary_markdown"] == "A corrected summary."
    assert "regenerating both" in capsys.readouterr().out


def test_a_complete_envelope_is_skipped_by_both_layers(tmp_path):
    paths = dirs(tmp_path)
    write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")
    summarize.run(**paths, client=StubClient())

    client = StubClient()
    result = summarize.run(**paths, client=client)

    assert result["summarized"] == []
    assert client.calls == [] and client.grounded_calls == []


def test_force_regenerates_both_layers(tmp_path):
    paths = dirs(tmp_path)
    write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")
    summarize.run(**paths, client=StubClient())

    client = StubClient(text="Regenerated.")
    summarize.run(**paths, client=client, force=True)

    assert len(client.calls) == 1 and len(client.grounded_calls) == 3
    assert envelope_at(paths)["summary_markdown"] == "Regenerated."


# --- --no-news ------------------------------------------------------------


def test_no_news_skips_the_grounded_calls_entirely(tmp_path):
    paths = dirs(tmp_path)
    write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")

    client = StubClient()
    summarize.run(**paths, client=client, with_news_layer=False)
    record = envelope_at(paths)

    assert client.grounded_calls == []
    assert record["news"] is None
    assert record["news_error"] is None, "skipping is not failing"


def test_no_news_leaves_the_news_to_be_backfilled_later(tmp_path):
    """A --no-news envelope owes news, so a normal run picks it up -- and
    still does not re-bill the summary."""
    paths = dirs(tmp_path)
    write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")
    summarize.run(**paths, client=StubClient(), with_news_layer=False)

    client = StubClient()
    summarize.run(**paths, client=client)

    assert client.calls == []
    assert envelope_at(paths)["news"] is not None


def test_no_news_does_not_rewrite_an_envelope_it_has_nothing_to_add_to(tmp_path):
    """Without collapsing back to two states, every --no-news run would
    rewrite every summary-only envelope forever."""
    paths = dirs(tmp_path)
    write_report(paths["reports_dir"], 2026, 2, "2026-09-15", "tuesday-waiver-wire")
    summarize.run(**paths, client=StubClient(), with_news_layer=False)

    client = StubClient()
    result = summarize.run(**paths, client=client, with_news_layer=False)

    assert result["summarized"] == []
    assert client.calls == []
