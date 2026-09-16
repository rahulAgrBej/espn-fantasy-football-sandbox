"""Discovery, prior selection, and reading a rendered report's header back.

These are the three places `espn_ff summarize` can be wrong without
anything failing: summarizing a report twice, handing the model the wrong
week's context, or writing an envelope whose provenance fields do not
describe the report they sit beside.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from espn_ff.ai import reports
from espn_ff.report.render import header_lines

# Deliberately not imported from cli: these tests pin the mapping itself, so
# reading it from the thing under test would make them pass by construction
# for a REPORTS table that had drifted.
INDEX = {
    ("monday", "monday-night-call"): "monday",
    ("tuesday", "week-in-review"): "tuesday",
    ("tuesday", "waiver-wire"): "tuesday-waivers",
    ("wednesday", "availability-watchlist"): "wednesday",
    ("thursday", "usage-and-market"): "thursday",
    ("friday", "lineup-lock"): "friday",
}


def _write_report(root, season, week, rendered_on, stem_tail, body="body\n"):
    path = root / str(season) / f"week-{week:02d}" / f"{rendered_on}-{stem_tail}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _write_summary(root, report):
    path = reports.summary_path(root, report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n")
    return path


# --- the filename -> report-type mapping ----------------------------------


def test_the_index_inverts_cli_reports():
    """The live table, not the local copy -- this is the one assertion that
    catches a REPORTS entry whose slug changed without this module knowing."""
    from espn_ff import cli

    assert reports.report_index() == INDEX
    assert len(reports.report_index()) == len(cli.REPORTS), "two report types share a (label, slug)"


def test_a_hyphenated_slug_maps_back_to_its_report_key(tmp_path):
    """`tuesday-waiver-wire.md` has to resolve to "tuesday-waivers", which no
    amount of splitting on hyphens will tell you -- the day label and the
    slug both carry them. Inverting the table is the only thing that does."""
    _write_report(tmp_path, 2026, 2, "2026-09-15", "tuesday-waiver-wire")
    _write_report(tmp_path, 2026, 2, "2026-09-15", "tuesday-week-in-review")

    found = {r.day: r for r in reports.scan(tmp_path, index=INDEX)}

    assert set(found) == {"tuesday-waivers", "tuesday"}
    assert found["tuesday-waivers"].slug == "waiver-wire"
    assert found["tuesday"].slug == "week-in-review"


def test_an_unrecognised_file_is_skipped_not_guessed_at(tmp_path):
    """A stray note or a renamed report type has no day key, so there is no
    prompt to build for it. Skipping beats inventing one."""
    _write_report(tmp_path, 2026, 2, "2026-09-15", "tuesday-waiver-wire")
    _write_report(tmp_path, 2026, 2, "2026-09-15", "friday-something-new")
    (tmp_path / "2026" / "week-02" / "notes.md").write_text("scratch\n")

    assert [r.stem for r in reports.scan(tmp_path, index=INDEX)] == [
        "2026-09-15-tuesday-waiver-wire"
    ]


# --- discovery ------------------------------------------------------------


def test_discovery_skips_reports_that_already_have_a_summary(tmp_path):
    """The idempotency guarantee. Both triggers land in the same job, so
    "already summarized" has to be the thing that stops the second one --
    not a lock, not an input, not a timestamp."""
    reports_dir, summaries_dir = tmp_path / "r", tmp_path / "s"
    _write_report(reports_dir, 2026, 1, "2026-09-08", "tuesday-week-in-review")
    _write_report(reports_dir, 2026, 2, "2026-09-15", "tuesday-week-in-review")

    week_one = reports.scan(reports_dir, index=INDEX)[0]
    _write_summary(summaries_dir, week_one)

    pending = reports.unsummarized(reports.scan(reports_dir, index=INDEX), summaries_dir)

    assert [r.week for r in pending] == [2]


def test_a_summary_in_either_tree_counts_as_summarized(tmp_path):
    """A run consults two trees: the pulled cache of what is already on S3,
    and the output tree holding what this run has written. Checking only the
    first would make idempotency depend on the S3 push having succeeded --
    two runs before a push would summarize the same report twice."""
    reports_dir, cache, out = tmp_path / "r", tmp_path / "cache", tmp_path / "out"
    _write_report(reports_dir, 2026, 2, "2026-09-15", "tuesday-waiver-wire")
    report = reports.scan(reports_dir, index=INDEX)[0]

    assert reports.unsummarized(reports.scan(reports_dir, index=INDEX), (cache, out)) == [report]

    _write_summary(out, report)
    assert reports.unsummarized(reports.scan(reports_dir, index=INDEX), (cache, out)) == []
    assert reports.unsummarized(reports.scan(reports_dir, index=INDEX), cache) == [report]


def test_force_re_summarizes_what_already_has_a_summary(tmp_path):
    reports_dir, summaries_dir = tmp_path / "r", tmp_path / "s"
    _write_report(reports_dir, 2026, 1, "2026-09-08", "tuesday-week-in-review")
    _write_summary(summaries_dir, reports.scan(reports_dir, index=INDEX)[0])

    assert reports.unsummarized(reports.scan(reports_dir, index=INDEX), summaries_dir) == []
    assert len(reports.unsummarized(reports.scan(reports_dir, index=INDEX), summaries_dir, force=True)) == 1


def test_day_and_week_narrow_the_candidates(tmp_path):
    reports_dir, summaries_dir = tmp_path / "r", tmp_path / "s"
    _write_report(reports_dir, 2026, 2, "2026-09-15", "tuesday-week-in-review")
    _write_report(reports_dir, 2026, 2, "2026-09-15", "tuesday-waiver-wire")
    _write_report(reports_dir, 2026, 1, "2026-09-08", "tuesday-waiver-wire")

    by_day = reports.unsummarized(reports.scan(reports_dir, index=INDEX), summaries_dir, day="tuesday-waivers")
    by_week = reports.unsummarized(reports.scan(reports_dir, index=INDEX), summaries_dir, week=1)

    assert {r.day for r in by_day} == {"tuesday-waivers"}
    assert len(by_day) == 2
    assert [r.stem for r in by_week] == ["2026-09-08-tuesday-waiver-wire"]


def test_a_missing_reports_directory_is_empty_not_an_error(tmp_path):
    """A first run, before pull-reports has ever populated the cache."""
    assert reports.scan(tmp_path / "never-created", index=INDEX) == []


def test_the_summary_path_mirrors_the_report_path_one_prefix_over(tmp_path):
    _write_report(tmp_path, 2026, 2, "2026-09-15", "tuesday-waiver-wire")
    report = reports.scan(tmp_path, index=INDEX)[0]

    path = reports.summary_path("/out", report)

    assert path.as_posix() == "/out/2026/week-02/2026-09-15-tuesday-waiver-wire.json"


# --- prior selection ------------------------------------------------------


def test_priors_take_the_newest_four_by_filename_date(tmp_path):
    for week, day_of_month in enumerate(range(1, 8), start=1):
        _write_report(tmp_path, 2026, week, f"2026-09-{day_of_month:02d}", "tuesday-week-in-review")

    all_reports = reports.scan(tmp_path, index=INDEX)
    latest = all_reports[-1]

    assert [r.week for r in reports.priors(all_reports, latest)] == [3, 4, 5, 6]


def test_priors_exclude_the_report_being_summarized(tmp_path):
    _write_report(tmp_path, 2026, 1, "2026-09-08", "tuesday-week-in-review")
    _write_report(tmp_path, 2026, 2, "2026-09-15", "tuesday-week-in-review")

    all_reports = reports.scan(tmp_path, index=INDEX)
    for report in all_reports:
        assert report.path not in {p.path for p in reports.priors(all_reports, report)}


def test_priors_never_cross_a_season_boundary(tmp_path):
    """A prior from last season compares this week against a roster, a
    league and a scoring system that no longer exist -- which reads as a
    change when it is a different league-year entirely."""
    _write_report(tmp_path, 2025, 17, "2025-12-23", "tuesday-week-in-review")
    _write_report(tmp_path, 2026, 1, "2026-09-08", "tuesday-week-in-review")

    all_reports = reports.scan(tmp_path, index=INDEX)
    week_one_2026 = [r for r in all_reports if r.season == 2026][0]

    assert reports.priors(all_reports, week_one_2026) == []


def test_priors_never_mix_report_types(tmp_path):
    """The waiver report and the week-in-review share a day label and a
    directory. Handing one the other's history would be invisible in the
    output and wrong in every sentence about what changed."""
    _write_report(tmp_path, 2026, 1, "2026-09-08", "tuesday-waiver-wire")
    _write_report(tmp_path, 2026, 1, "2026-09-08", "tuesday-week-in-review")
    _write_report(tmp_path, 2026, 2, "2026-09-15", "tuesday-waiver-wire")

    all_reports = reports.scan(tmp_path, index=INDEX)
    week_two_waivers = [r for r in all_reports if r.week == 2][0]
    selected = reports.priors(all_reports, week_two_waivers)

    assert [r.slug for r in selected] == ["waiver-wire"]


def test_the_first_report_of_a_season_has_no_priors(tmp_path):
    _write_report(tmp_path, 2026, 1, "2026-09-08", "tuesday-week-in-review")
    all_reports = reports.scan(tmp_path, index=INDEX)

    assert reports.priors(all_reports, all_reports[0]) == []


# --- the header block -----------------------------------------------------


def test_parse_header_round_trips_a_header_built_by_render(tmp_path):
    """Pins the 5-line contract across the two modules that do not import
    each other: render.header_lines writes it, reports.parse_header reads
    it back, and the envelope's provenance fields come from the second."""
    et = ZoneInfo("America/New_York")
    window = (datetime(2026, 9, 15, 3, tzinfo=et), datetime(2026, 9, 22, 3, tzinfo=et))
    built = header_lines(
        title="Waiver wire and opening market -- 2026 week 2",
        week=2,
        covers="week 2's waiver window",
        window=window,
        rendered_at=1789527480,
    )

    parsed = reports.parse_header("\n".join(built) + "\n\n## Freshness\n- espn: never\n")

    assert parsed["title"] == "Waiver wire and opening market -- 2026 week 2"
    assert parsed["covers"] == "week 2's waiver window"
    assert parsed["week"] == 2
    assert parsed["week_window"] == built[3].removeprefix("**Week 2** ")
    assert parsed["rendered"] == built[4].removeprefix("**Rendered** ")


def test_parse_header_tolerates_a_report_with_no_header():
    """A report whose header drifted still gets summarized -- it just
    carries less provenance. Failing here would mean a formatting change
    silently stopped every summary."""
    parsed = reports.parse_header("no header at all\n")

    assert parsed == {
        "title": None,
        "covers": None,
        "week": None,
        "week_window": None,
        "rendered": None,
    }


def test_parse_header_ignores_a_rendered_line_deep_in_the_body():
    """Only the first 20 lines are scanned, so a table cell or a quoted
    example further down cannot masquerade as the dateline."""
    text = "\n".join(
        ["# Title", "", "**Covers** the real one", "**Week 2** window", "**Rendered** real"]
        + ["filler"] * 30
        + ["**Rendered** an impostor deep in the body"]
    )

    assert reports.parse_header(text)["rendered"] == "real"


def test_the_report_dataclass_sorts_by_filename_date_never_mtime(tmp_path):
    """Both inputs arrive by `aws s3 sync`, which stamps a fresh mtime on a
    file whose contents are weeks old -- so mtime here would not merely be
    unreliable, it would be actively inverted."""
    old = _write_report(tmp_path, 2026, 1, "2026-09-08", "tuesday-week-in-review")
    _write_report(tmp_path, 2026, 2, "2026-09-15", "tuesday-week-in-review")

    # Touch the older report so its mtime is the newest on disk.
    old.write_text("re-synced, same content date\n")

    assert [r.week for r in reports.scan(tmp_path, index=INDEX)] == [1, 2]
    assert reports.scan(tmp_path, index=INDEX)[0].rendered_on == date(2026, 9, 8)
