"""The JSON payload helpers in espn_ff/report/payload.py, plus the tests that
keep tests/payload_helpers.py's drift guard honest.

Eight day modules trust that guard to catch a section added to `render` and
forgotten in `payload`. A guard that silently passes everything would make all
eight of those tests decorative, so the last block here asserts it actually
fails on a dropped section, a reordering, and a leaked display string.
"""

import json

import pandas as pd
import pytest

from espn_ff.report import payload as payload_lib

from payload_helpers import (
    assert_no_display_strings,
    assert_payload_matches_markdown,
    payload_headings,
)


# ---- clean --------------------------------------------------------------

@pytest.mark.parametrize("value", [None, float("nan"), pd.NaT, pd.NA])
def test_every_flavour_of_missing_becomes_none(value):
    """The report package uses these interchangeably for "no reading".
    Preserving the difference would export which feed happened to produce the
    column."""
    assert payload_lib.clean(value) is None


def test_numpy_scalars_are_unwrapped_to_python():
    import numpy as np

    assert payload_lib.clean(np.int64(3)) == 3
    assert isinstance(payload_lib.clean(np.int64(3)), int)
    assert payload_lib.clean(np.float64(1.5)) == 1.5
    assert payload_lib.clean(np.bool_(True)) is True
    assert payload_lib.clean(np.float64("nan")) is None


def test_clean_keeps_real_values_including_falsy_ones():
    """0 and False are readings, not absences -- collapsing them to null is
    the bug this test exists to pin."""
    assert payload_lib.clean(0) == 0
    assert payload_lib.clean(0.0) == 0.0
    assert payload_lib.clean(False) is False
    assert payload_lib.clean("") == ""


def test_timestamps_become_iso_strings():
    assert payload_lib.clean(pd.Timestamp("2026-09-16 11:06")) == "2026-09-16T11:06:00"


# ---- rows ---------------------------------------------------------------

def test_rows_keys_on_the_declared_columns_only():
    columns = [payload_lib.column("a", "A", "integer")]
    frame = pd.DataFrame([{"a": 1, "unwanted": 2}])
    assert payload_lib.rows(frame, columns) == [{"a": 1}]


def test_rows_of_an_empty_frame_is_an_empty_list():
    columns = [payload_lib.column("a", "A", "integer")]
    assert payload_lib.rows(pd.DataFrame(), columns) == []
    assert payload_lib.rows(None, columns) == []


def test_rows_tolerates_a_column_absent_from_the_frame():
    """Same tolerance render.table shows when it prints `--` -- a missing
    column degrades one cell, it does not fail the report."""
    columns = [payload_lib.column("a", "A", "integer"), payload_lib.column("b", "B", "integer")]
    assert payload_lib.rows(pd.DataFrame([{"a": 1}]), columns) == [{"a": 1, "b": None}]


def test_rows_accepts_a_list_of_dicts_as_well_as_a_frame():
    columns = [payload_lib.column("a", "A", "integer")]
    assert payload_lib.rows([{"a": 1}, {"a": 2}], columns) == [{"a": 1}, {"a": 2}]


def test_rows_cleans_nan_to_null():
    columns = [payload_lib.column("a", "A", "number")]
    out = payload_lib.rows(pd.DataFrame([{"a": float("nan")}]), columns)
    assert out == [{"a": None}]


# ---- header_block -------------------------------------------------------

def test_header_block_carries_both_iso_endpoints_and_the_display_string():
    window = (pd.Timestamp("2026-09-15 03:00", tz="America/New_York"),
              pd.Timestamp("2026-09-22 03:00", tz="America/New_York"))
    header = payload_lib.header_block("T", 2, "covers", window, 1_760_000_000)
    assert header["week_window"]["start"].startswith("2026-09-15T03:00")
    assert header["week_window"]["display"] == "Tue 2026-09-15 03:00 - Tue 2026-09-22 03:00 ET"
    assert header["week"] == 2 and header["title"] == "T"


def test_header_block_with_no_calendar_says_so_rather_than_omitting_the_window():
    """Mirrors render.header_lines: a missing line is indistinguishable from a
    report that had nothing to say, so the words are rendered instead."""
    header = payload_lib.header_block("T", 2, "covers", None, 1_760_000_000)
    assert header["week_window"] == {"start": None, "end": None, "display": "insufficient data"}


def test_header_rendered_display_matches_renders_own_formatting():
    from espn_ff.report import render as render_lib

    header = payload_lib.header_block("T", 2, "c", None, 1_760_000_000)
    assert header["rendered_display"] == render_lib._fmt_rendered_at(1_760_000_000)


# ---- freshness ----------------------------------------------------------

def test_freshness_block_keeps_the_fixed_feed_order():
    fresh = {"odds": (1, False), "sleeper": (2, True), "espn": (3, False), "nflverse": (4, False)}
    assert [f["feed"] for f in payload_lib.freshness_block(fresh)] == [
        "sleeper", "nflverse", "espn", "odds",
    ]


def test_freshness_block_tolerates_a_short_dict():
    """Saturday drops `odds` entirely."""
    feeds = payload_lib.freshness_block({"sleeper": (1, False)})
    assert [f["feed"] for f in feeds] == ["sleeper"]


def test_freshness_block_renders_never_for_a_feed_that_has_not_run():
    feeds = payload_lib.freshness_block({"sleeper": (None, True)})
    assert feeds[0]["at"] is None and feeds[0]["at_display"] == "never"
    assert feeds[0]["stale"] is True


# ---- sections -----------------------------------------------------------

def test_insufficient_is_its_own_kind_not_an_empty_table():
    """"could not compute, here is why" and "computed, found nothing" are
    different facts -- docs/report-weekly-schedule.md requires the difference
    survive."""
    section = payload_lib.insufficient_section("x", "X", "no export")
    assert section["kind"] == "insufficient" and section["reason"] == "no export"


def test_headingless_blocks_are_allowed():
    block = payload_lib.table_section("x", None, [], [], level=None)
    assert block["heading"] is None and block["level"] is None


def test_section_data_is_cleaned():
    section = payload_lib.prose_section("x", "X", [], data={"n": float("nan")})
    assert section["data"] == {"n": None}


def test_blocks_section_nests_recursively():
    inner = payload_lib.table_section("inner", "Inner", [], [], level=4)
    mid = payload_lib.blocks_section("mid", "Mid", [inner], level=3)
    outer = payload_lib.blocks_section("outer", "Outer", [mid])
    assert payload_headings([outer]) == [(2, "Outer"), (3, "Mid"), (4, "Inner")]


# ---- envelope -----------------------------------------------------------

def _envelope():
    return payload_lib.envelope(
        season=2026, week=2, day="wednesday", day_label="wednesday",
        slug="availability-watchlist", stem="2026-09-16-wednesday-availability-watchlist",
        generated_at="2026-09-16T11:06:00-04:00",
        header=payload_lib.header_block("T", 2, "c", None, 1_760_000_000),
        sections=[payload_lib.list_section("cannot-see", "What this report cannot see", ["a"])],
        markdown="# T\n\nbody\n",
    )


def test_envelope_embeds_the_markdown_verbatim_and_hashes_it():
    """The no-information-loss guarantee: whatever the structuring missed is
    still in the envelope, and the hash says whether it is the same bytes as
    the .md in the bucket."""
    envelope = _envelope()
    assert envelope["markdown"] == "# T\n\nbody\n"
    assert envelope["markdown_sha256"] == payload_lib.sha256("# T\n\nbody\n")


def test_envelope_related_paths_are_the_bucket_keys_not_local_paths():
    related = _envelope()["related"]
    assert related["markdown_path"] == (
        "reports/2026/week-02/2026-09-16-wednesday-availability-watchlist.md"
    )
    assert related["summary_path"] == (
        "summaries/2026/week-02/2026-09-16-wednesday-availability-watchlist.json"
    )


def test_envelope_is_serializable():
    json.dumps(_envelope())


def test_envelope_declares_its_schema_version():
    assert _envelope()["schema_version"] == payload_lib.SCHEMA_VERSION


# ---- the guard guards ---------------------------------------------------

def test_drift_guard_fails_when_a_section_is_missing_from_the_payload():
    """If this passes with a section deliberately dropped, the guard is
    decorative and every day module's drift test is worthless."""
    text = "# T\n\n## Kept\n\n## Dropped\n"
    sections = [payload_lib.prose_section("kept", "Kept", [])]
    with pytest.raises(AssertionError):
        assert_payload_matches_markdown(text, sections)


def test_drift_guard_fails_on_reordering():
    text = "# T\n\n## A\n\n## B\n"
    sections = [payload_lib.prose_section("b", "B", []), payload_lib.prose_section("a", "A", [])]
    with pytest.raises(AssertionError):
        assert_payload_matches_markdown(text, sections)


def test_drift_guard_ignores_the_h1_dateline():
    text = "# Availability watchlist -- 2026 week 2\n\n## Freshness\n"
    assert_payload_matches_markdown(text, [payload_lib.prose_section("freshness", "Freshness", [])])


def test_display_string_guard_catches_a_markdown_placeholder():
    section = payload_lib.table_section(
        "x", "X", [payload_lib.column("a", "A", "number")], [{"a": "--"}],
    )
    with pytest.raises(AssertionError):
        assert_no_display_strings([section])


def test_clean_recurses_into_containers_rather_than_stringifying_them():
    """A genuine list -- friday's fired rules, its unfilled slots -- must
    survive as a list. `str(["a"])` produces "['a']", which looks plausible
    and is unparseable."""
    assert payload_lib.clean(["a", float("nan"), 1]) == ["a", None, 1]
    assert payload_lib.clean({"k": float("nan")}) == {"k": None}
    assert payload_lib.clean(("a", "b")) == ["a", "b"]


# ---- unset: the placeholder collapse ------------------------------------

@pytest.mark.parametrize("value", ["--", "—", "insufficient data", "— / — / —", "-- / -- / --"])
def test_unset_collapses_every_no_reading_placeholder(value):
    """availability.read really does put the literal "insufficient data" in
    the `tier` column, and format_trajectory really does emit three em
    dashes -- both observed in a live 2026 week-2 render."""
    assert payload_lib.unset(value) is None


def test_unset_leaves_real_values_alone():
    assert payload_lib.unset("OUT") == "OUT"
    assert payload_lib.unset("DNP / — / —") == "DNP / — / —"   # partial signal is a reading
    assert payload_lib.unset(0.0) == 0.0


def test_clean_does_not_collapse_placeholders():
    """Prose bodies and notes keep these strings verbatim -- there the words
    are the report's own phrasing, and stripping them empties the sentence."""
    assert payload_lib.clean("insufficient data") == "insufficient data"


def test_rows_applies_unset_not_just_clean():
    columns = [payload_lib.column("tier", "tier", "string")]
    assert payload_lib.rows([{"tier": "insufficient data"}], columns) == [{"tier": None}]
