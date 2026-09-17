"""The roster-freshness gate.

These cover the 2026-09-17 incident directly: a trade made Wednesday
afternoon, a Thursday report rendered Thu 11:01 ET that still listed the
traded-away player as rostered, and a Freshness header that printed
`espn: 2026-09-16 11:02 ET` without flagging anything -- because the only
staleness signal a report had was `_espn_freshness`'s max across every view
against a flat 24-hour threshold, and 23h59m is inside 24 hours.

The `espn_view_freshness` tests these build on live in
tests/test_report_waivers.py, under the transactions gate that first needed
a per-view answer.
"""

import json
from datetime import datetime

import pytest

from espn_ff import cache
from espn_ff.report import loaders
from espn_ff.weeks import ET

RENDERED_AT = datetime(2026, 9, 17, 11, 1, tzinfo=ET).timestamp()

ROSTER_SLUG = cache._slug("-".join(sorted(loaders._ROSTER_VIEWS)))


def _raw_dir(tmp_path, monkeypatch, **views):
    """A data/raw/<season>/ tree holding one .meta.json per named view."""
    season_dir = tmp_path / "2026"
    season_dir.mkdir()
    for slug, fetched_at in views.items():
        (season_dir / f"{slug}-aaaa.meta.json").write_text(json.dumps({"fetched_at": fetched_at}))
    monkeypatch.setattr(loaders.config, "RAW_DIR", tmp_path)
    return season_dir


def _week_meta(season_dir, week, fetched_at, key):
    """One mRoster sidecar for a specific scoring period. mRoster is the only
    ESPN view in this repo cached per week, so its sidecars are only
    distinguishable by the scoringPeriodId in the recorded url."""
    (season_dir / f"{ROSTER_SLUG}-{key}.meta.json").write_text(json.dumps({
        "fetched_at": fetched_at,
        "url": f"https://lm-api-reads.fantasy.espn.com/...?view=mRoster&scoringPeriodId={week}",
    }))


def test_roster_fetched_for_this_render_is_current(tmp_path, monkeypatch):
    _raw_dir(tmp_path, monkeypatch, **{ROSTER_SLUG: RENDERED_AT - 30})
    assert loaders.roster_read_is_current(RENDERED_AT, season=2026) == (True, None)
    assert loaders.roster_staleness_note(RENDERED_AT, season=2026) is None


def test_yesterdays_roster_is_not_current_and_says_when_it_was_fetched(tmp_path, monkeypatch):
    """The incident. The roster payload is 23h59m old -- inside the flat
    24-hour ESPN_STALE_HOURS window that let it through unflagged -- and it
    predates the trade it is missing."""
    fetched = datetime(2026, 9, 16, 11, 2, tzinfo=ET).timestamp()
    _raw_dir(tmp_path, monkeypatch, **{ROSTER_SLUG: fetched})

    current, reason = loaders.roster_read_is_current(RENDERED_AT, season=2026)
    assert current is False
    assert "Wed 2026-09-16 11:02 ET" in reason
    assert "24.0 hours before this render" in reason

    note = loaders.roster_staleness_note(RENDERED_AT, season=2026)
    assert note.startswith("The roster tables above were not re-pulled for this render")
    assert note.endswith(".")
    # The note names the subject once; `reason` supplies the evidence.
    assert note.count("roster payload") == 1


def test_the_note_never_claims_a_two_hour_old_roster_is_not_this_mornings(tmp_path, monkeypatch):
    """The wording must be true at every age past the threshold, not only at
    the 26-hour one. The case this fires on most often is the in-process
    refresh dying at 11:00 after `espn-daily` landed at 09:08 -- 1.9 hours
    old, past the bound, and unambiguously this morning's. A gate that prints
    a false claim is worse than no gate."""
    _raw_dir(tmp_path, monkeypatch, **{ROSTER_SLUG: RENDERED_AT - 2 * 3600})

    note = loaders.roster_staleness_note(RENDERED_AT, season=2026)
    assert note is not None, "a 2-hour-old payload is past ROSTER_MAX_AGE_SECONDS"
    assert "this morning" not in note
    # It still has to say how old, or the reader cannot tell 2 hours from 26.
    assert "2.0 hours before this render" in note


def test_a_fresh_other_view_does_not_make_a_stale_roster_look_current(tmp_path, monkeypatch):
    """Why this cannot be `_espn_freshness`. A player-pool or
    platform-settings fetch minutes ago marks the ESPN *feed* fresh while the
    roster behind weekly-rosters.csv is a day old."""
    pool_slug = cache._slug("kona_player_info")
    _raw_dir(
        tmp_path, monkeypatch,
        **{ROSTER_SLUG: RENDERED_AT - 86_400, pool_slug: RENDERED_AT - 60},
    )

    assert loaders._espn_freshness(season=2026) == (pytest.approx(RENDERED_AT - 60), False)
    assert loaders.roster_read_is_current(RENDERED_AT, season=2026)[0] is False


def test_never_fetched_roster_is_not_current(tmp_path, monkeypatch):
    _raw_dir(tmp_path, monkeypatch)
    current, reason = loaders.roster_read_is_current(RENDERED_AT, season=2026)
    assert current is False
    assert reason == "the ESPN roster payload has never been fetched"


def test_the_gate_reads_the_export_view_tuple_not_the_pull_one(tmp_path, monkeypatch):
    """`cmd_pull` fetches ["mRoster","mTeam","mMatchupScore"] and
    `cmd_export` fetches ["mRoster","mTeam"] -- different cache keys.
    weekly-rosters.csv is built from the export's fetch, so a fresh pull
    payload must NOT satisfy this gate."""
    pull_slug = cache._slug("-".join(sorted(["mRoster", "mTeam", "mMatchupScore"])))
    assert pull_slug != ROSTER_SLUG
    _raw_dir(tmp_path, monkeypatch, **{pull_slug: RENDERED_AT - 30})
    assert loaders.roster_read_is_current(RENDERED_AT, season=2026)[0] is False


def test_a_refetched_completed_week_does_not_mask_the_displayed_weeks_roster(tmp_path, monkeypatch):
    """mRoster is cached one file per scoring period, so the bare glob's
    max() answers "when did ANY week's roster last land". A `--refresh-weeks
    1` refetch would satisfy that while week 3's roster -- the one the report
    actually displays -- is a day old."""
    season_dir = _raw_dir(tmp_path, monkeypatch)
    _week_meta(season_dir, week=1, fetched_at=RENDERED_AT - 60, key="aaaa")
    _week_meta(season_dir, week=3, fetched_at=RENDERED_AT - 86_400, key="bbbb")

    # Week-blind: the completed week's fresh refetch wins the max.
    assert loaders.roster_read_is_current(RENDERED_AT, season=2026)[0] is True
    # Week-aware: the displayed week's own payload is what gets measured.
    assert loaders.roster_read_is_current(RENDERED_AT, season=2026, week=3)[0] is False
    assert loaders.roster_read_is_current(RENDERED_AT, season=2026, week=1)[0] is True


def test_a_week_with_no_cached_roster_is_not_current(tmp_path, monkeypatch):
    """A scoring period that rolled over before any pull ran has no sidecar
    at all -- the report's roster slice comes back empty and it must say so,
    not inherit last week's freshness."""
    season_dir = _raw_dir(tmp_path, monkeypatch)
    _week_meta(season_dir, week=2, fetched_at=RENDERED_AT - 60, key="aaaa")

    current, reason = loaders.roster_read_is_current(RENDERED_AT, season=2026, week=3)
    assert current is False
    assert reason == "the ESPN roster payload has never been fetched"


def test_the_gate_is_order_independent(tmp_path, monkeypatch):
    """`cache_path` sorts views before slugging, so reordering
    `_ROSTER_VIEWS` must not silently break the lookup into always-stale."""
    _raw_dir(tmp_path, monkeypatch, **{ROSTER_SLUG: RENDERED_AT - 30})
    monkeypatch.setattr(loaders, "_ROSTER_VIEWS", ["mTeam", "mRoster"])
    assert loaders.roster_read_is_current(RENDERED_AT, season=2026) == (True, None)
