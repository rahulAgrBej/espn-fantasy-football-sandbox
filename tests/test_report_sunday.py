"""Sunday's pre-lock report -- the pre_lock gate (including the prior-day
rung no other module has), the last-two-captures line read, the market-ranked
undecided slots, the OUT buckets, the kickoff clock, and the render/build
wiring. Fixtures only, no network, no disk beyond the tmp_path-scoped parquet
the gate tests need."""

from datetime import date

import pandas as pd
import pytest

from espn_ff.report import sunday


# ---- fixture builders (same shapes as tests/test_report_friday.py) ---------

def _roster_row(player_id, player_name, position, lineup_slot, started,
                week=3, team_id=5, injury_status="ACTIVE", pro_team="KC"):
    return {
        "season": 2026, "week": week, "team_id": team_id, "team_name": "Us",
        "player_id": player_id, "player_name": player_name, "position": position,
        "pro_team": pro_team, "lineup_slot_id": 0, "lineup_slot": lineup_slot,
        "started": started, "injury_status": injury_status, "acquisition_type": "DRAFT",
        "points": None, "projected": None,
    }


def _pool_row(player_id, player_name, position, eligible_slots, week_projected, week=3):
    return {
        "player_id": player_id, "player_name": player_name, "position": position,
        "pro_team": "KC", "eligible_slots": eligible_slots, "week": week,
        "week_projected": week_projected, "percent_owned": 50.0,
        "season_points": 0.0, "season_projected": 0.0,
    }


def _slim_row(sleeper_id, injury_status=None, practice_participation=None,
              status="Active", active=True):
    return {
        "sleeper_id": sleeper_id, "espn_id": None, "gsis_id": None, "full_name": None,
        "team": "KC", "position": "RB", "depth_chart_position": None, "depth_chart_order": None,
        "injury_status": injury_status, "injury_body_part": None, "injury_notes": None,
        "practice_participation": practice_participation, "status": status, "active": active,
        "number": None, "years_exp": None, "fetched_at": None,
    }


def _sleeper_map(pairs):
    return pd.DataFrame([{"espn_player_id": e, "sleeper_id": s} for e, s in pairs])


def _totals_rows(captured_at, week, min_spread, gb_spread, total):
    return [
        {"captured_at": captured_at, "week": week, "event_id": "e1", "team": "MIN",
         "market": "spreads", "book": "draftkings", "outcome_name": "Minnesota Vikings", "point": min_spread},
        {"captured_at": captured_at, "week": week, "event_id": "e1", "team": "GB",
         "market": "spreads", "book": "draftkings", "outcome_name": "Green Bay Packers", "point": gb_spread},
        {"captured_at": captured_at, "week": week, "event_id": "e1", "team": None,
         "market": "totals", "book": "draftkings", "outcome_name": "Over", "point": total},
    ]


def _prop_rows(captured_at, week, player, point):
    return [
        {"captured_at": captured_at, "week": week, "event_id": "e1", "player_name": player,
         "team": "GB", "market": "player_receptions", "book": "draftkings",
         "outcome_name": side, "price": -110, "point": point}
        for side in ("Over", "Under")
    ]


_SUNDAY = date(2026, 9, 20)
# 2026-09-20 11:30 ET, so minutes_to_lock has 90 minutes to a 13:00 kickoff.
_RENDERED_AT = pd.Timestamp("2026-09-20 11:30", tz="America/New_York").timestamp()

_EMPTY_GATE = {
    "insufficient": True, "reason": "no pre_lock entry in last_run.json",
    "ran_at": None, "credits_spent": None, "stale_flag": None,
}
_EMPTY_LINES = {
    "insufficient": True, "reason": "no team_totals.parquet on disk", "rows": [],
    "current_at": None, "prior_at": None, "from_this_run": False,
}
_EMPTY_PROPS = {
    "insufficient": True, "reason": "no player_props.parquet on disk", "captured_at": None,
    "by_player": {}, "unmatched_count": 0, "from_this_run": False,
}
_EMPTY_OUT = {
    "gate": {"insufficient": True, "reason": "no Sleeper slim snapshots on disk",
             "baseline_date": None, "current_date": None, "days_used": None},
    "moved": [], "already_out": [], "unmatched_names": set(),
    "fallback_names": set(), "nan_names": set(),
}
_EMPTY_RECOMMENDED = {
    "insufficient": True, "reason": "no weekly-rosters or roster-slots export for this week",
    "lineup": [], "unfilled_slots": [], "unprojected_names": set(), "fallback_names": set(),
    "candidates": [], "eligibility": [], "values": [],
}
_LOCK = {
    "kickoff_at": None, "kickoff_label": "13:00 ET", "minutes": 90,
    "source": "nflverse schedules", "reason": None,
}


def _render(**overrides):
    """render() with every argument defaulted to its empty/insufficient
    shape, so a test names only the fields it cares about."""
    kwargs = dict(
        season=2026, week=3, team_id=5,
        lock=dict(_LOCK), gate=dict(_EMPTY_GATE), lines=dict(_EMPTY_LINES),
        props=dict(_EMPTY_PROPS), market_by_player={}, undecided_rows=[],
        out_read=dict(_EMPTY_OUT), recommended=dict(_EMPTY_RECOMMENDED),
        footer_notes=sunday.FOOTER_NOTES, rendered_at=_RENDERED_AT,
    )
    kwargs.update(overrides)
    return sunday.render(**kwargs)


def _last_run(monkeypatch, entry):
    monkeypatch.setattr(sunday.odds_store, "read_last_run", lambda job=None: entry)


def _ran_at(hour=10, minute=38):
    return pd.Timestamp(f"2026-09-20 {hour:02d}:{minute:02d}", tz="America/New_York").timestamp()


# ---- pre_lock_read: one test per rung of the ladder ------------------------

def test_pre_lock_read_no_entry_names_friday_as_what_would_render_instead(monkeypatch):
    """The headline trap: with no pre_lock entry, team_totals.parquet still
    holds Friday's line_movement capture, so the market sections would render
    normally off a three-day-old line."""
    _last_run(monkeypatch, None)
    gate = sunday.pre_lock_read(today=_SUNDAY)
    assert gate["insufficient"] is True
    assert "no pre_lock entry in last_run.json" in gate["reason"]
    assert "Friday's line_movement capture" in gate["reason"]


def test_pre_lock_read_stale_entry_is_the_budget_abort_case(monkeypatch):
    _last_run(monkeypatch, {"ran_at": _ran_at(), "stale": True, "reason": "reserve exhausted"})
    gate = sunday.pre_lock_read(today=_SUNDAY)
    assert gate["insufficient"] is True
    assert "marked stale" in gate["reason"]
    assert "reserve exhausted" in gate["reason"]


def test_pre_lock_read_entry_from_a_prior_day_is_insufficient_even_with_stale_false(monkeypatch):
    """The rung no other gate in this repo has. Last Sunday's entry carries
    stale=False and is identical to this morning's in every field but its
    date -- only the date separates "the market moved" from "you are reading
    a week-old line"."""
    last_sunday = pd.Timestamp("2026-09-13 10:38", tz="America/New_York").timestamp()
    _last_run(monkeypatch, {"ran_at": last_sunday, "stale": False, "credits_spent": 21})
    gate = sunday.pre_lock_read(today=_SUNDAY)
    assert gate["insufficient"] is True
    assert "not today" in gate["reason"]
    assert "2026-09-13" in gate["reason"]


def test_pre_lock_read_success_carries_ran_at_and_credits_spent(monkeypatch):
    _last_run(monkeypatch, {"ran_at": _ran_at(), "stale": False, "credits_spent": 21})
    gate = sunday.pre_lock_read(today=_SUNDAY)
    assert gate["insufficient"] is False
    assert gate["credits_spent"] == 21
    assert gate["stale_flag"] is False


# ---- featured_lines --------------------------------------------------------

def _good_gate():
    return {"insufficient": False, "reason": None, "ran_at": _ran_at(),
            "credits_spent": 21, "stale_flag": False}


def test_featured_lines_short_circuits_on_the_gates_own_reason(tmp_path, monkeypatch):
    """One reason string flows to every odds section -- the reader never sees
    three different explanations for one failure."""
    monkeypatch.setattr(sunday.config, "ODDS_TEAM_TOTALS", tmp_path / "team_totals.parquet")
    result = sunday.featured_lines(3, dict(_EMPTY_GATE))
    assert result["insufficient"] is True
    assert result["reason"] == _EMPTY_GATE["reason"]


def test_featured_lines_single_capture_still_renders_the_lines_with_no_delta(tmp_path, monkeypatch):
    """The deliberate divergence from friday.line_movement_read: Friday's
    section *is* the diff so one capture has nothing to say, but this section
    is "the pre_lock job's featured lines" -- the absolute numbers are the
    deliverable."""
    path = tmp_path / "team_totals.parquet"
    pd.DataFrame(_totals_rows("2026-09-20T14:38:00+00:00", 3, -2.5, 2.5, 44.5)).to_parquet(path)
    monkeypatch.setattr(sunday.config, "ODDS_TEAM_TOTALS", path)

    result = sunday.featured_lines(3, _good_gate())

    assert result["insufficient"] is False
    assert len(result["rows"]) == 2
    assert all(r["implied_delta"] is None for r in result["rows"])
    assert result["prior_at"] is None


def test_featured_lines_diffs_the_last_two_captures_not_first_against_last(tmp_path, monkeypatch):
    """By Sunday this parquet holds three captures for the week -- Tuesday's
    slate, Friday's line_movement, this morning's pre_lock. Reusing Friday's
    captures[0]-vs-captures[-1] slice would render "movement since Tuesday's
    open" under a Sunday heading."""
    path = tmp_path / "team_totals.parquet"
    rows = (
        _totals_rows("2026-09-15T13:38:00+00:00", 3, -1.0, 1.0, 44.5)     # Tue slate
        + _totals_rows("2026-09-18T14:08:00+00:00", 3, -2.5, 2.5, 44.5)   # Fri line_movement
        + _totals_rows("2026-09-20T14:38:00+00:00", 3, -4.0, 4.0, 44.5)   # Sun pre_lock
    )
    pd.DataFrame(rows).to_parquet(path)
    monkeypatch.setattr(sunday.config, "ODDS_TEAM_TOTALS", path)

    result = sunday.featured_lines(3, _good_gate())

    assert result["current_at"] == "2026-09-20T14:38:00+00:00"
    assert result["prior_at"] == "2026-09-18T14:08:00+00:00"
    min_row = next(r for r in result["rows"] if r["team"] == "MIN")
    # Friday implied 23.5 -> Sunday 24.25. Against Tuesday's 22.75 the delta
    # would have been 1.5, which is the bug this test exists to catch.
    assert min_row["implied_delta"] == pytest.approx(0.75)


def test_featured_lines_flags_a_capture_that_predates_this_runs_ran_at(tmp_path, monkeypatch):
    """The spec's named trap: a budget-aborted pre_lock writes a fresh ran_at
    over unchanged data, leaving Friday's lines looking identical to a fresh
    pull. Timestamp agreement is the only instrument available."""
    path = tmp_path / "team_totals.parquet"
    pd.DataFrame(_totals_rows("2026-09-18T14:08:00+00:00", 3, -2.5, 2.5, 44.5)).to_parquet(path)
    monkeypatch.setattr(sunday.config, "ODDS_TEAM_TOTALS", path)

    result = sunday.featured_lines(3, _good_gate())

    assert result["insufficient"] is False
    assert result["from_this_run"] is False


def test_featured_lines_capture_matching_ran_at_reads_as_this_run(tmp_path, monkeypatch):
    path = tmp_path / "team_totals.parquet"
    pd.DataFrame(_totals_rows("2026-09-20T14:38:00+00:00", 3, -2.5, 2.5, 44.5)).to_parquet(path)
    monkeypatch.setattr(sunday.config, "ODDS_TEAM_TOTALS", path)

    assert sunday.featured_lines(3, _good_gate())["from_this_run"] is True


# ---- pre_lock_props --------------------------------------------------------

def test_pre_lock_props_uses_only_the_newest_capture(tmp_path, monkeypatch):
    """Thursday's props_primary capture and this morning's pre_lock capture
    share one parquet; a player quoted only on Thursday must not appear in a
    table headed "pre-lock"."""
    path = tmp_path / "player_props.parquet"
    rows = (
        _prop_rows("2026-09-17T14:08:00+00:00", 3, "Thursday Only", 5.5)
        + _prop_rows("2026-09-17T14:08:00+00:00", 3, "Jayden Reed", 4.5)
        + _prop_rows("2026-09-20T14:38:00+00:00", 3, "Jayden Reed", 6.5)
    )
    pd.DataFrame(rows).to_parquet(path)
    monkeypatch.setattr(sunday.config, "ODDS_PROPS", path)
    monkeypatch.setattr(sunday.odds_projections.store, "read_league_scoring",
                        lambda: [{"stat_abbrev": "REC", "points": 1.0}])
    monkeypatch.setattr(sunday.odds_projections.config, "NFLVERSE_XWALK", tmp_path / "x.csv")
    monkeypatch.setattr(sunday.odds_projections.config, "SLEEPER_ID_MAP", tmp_path / "m.csv")
    pool_df = pd.DataFrame([
        {"player_id": 1, "player_name": "Jayden Reed", "pro_team": "GB"},
        {"player_id": 2, "player_name": "Thursday Only", "pro_team": "GB"},
    ])

    result = sunday.pre_lock_props(3, _good_gate(), pool_df)

    assert result["insufficient"] is False
    assert result["captured_at"] == "2026-09-20T14:38:00+00:00"
    assert set(result["by_player"]) == {1}
    assert result["by_player"][1]["points"] == pytest.approx(6.5)


# ---- undecided_slots / ranked_undecided ------------------------------------

_ROSTER_SLOTS = pd.DataFrame([
    {"slot": "QB", "count": 1}, {"slot": "RB", "count": 2}, {"slot": "WR", "count": 2},
    {"slot": "RB/WR", "count": 1}, {"slot": "TE", "count": 1}, {"slot": "D/ST", "count": 1},
    {"slot": "K", "count": 1}, {"slot": "Bench", "count": 6}, {"slot": "IR", "count": 1},
])
_ALLOWED = {"QB", "RB", "WR", "RB/WR", "TE", "D/ST", "K", "Bench", "IR"}


def _two_wr_recommended():
    """A minimal recommended-lineup working set with two WR-eligible players:
    the incumbent projects higher, the alternative is priced higher."""
    return {
        "insufficient": False, "reason": None, "lineup": [
            {"slot": "WR", "player_id": 1, "player_name": "Incumbent", "projection": 12.0},
        ],
        "unfilled_slots": [], "unprojected_names": set(), "fallback_names": set(),
        "candidates": [
            {"player_id": 1, "player_name": "Incumbent"},
            {"player_id": 2, "player_name": "Alternative"},
        ],
        "eligibility": [{"WR"}, {"WR"}],
        "values": [12.0, 10.0],
    }


def test_ranked_undecided_ranks_by_pre_lock_consensus_not_espn_projection():
    """The whole reason this section renders at 11:30 rather than being
    settled on Friday: the market and the projection can disagree."""
    held = [{"slot": "WR", "player_name": "Incumbent", "reason": "Incumbent is COIN_FLIP"}]
    market = {
        1: {"points": 8.0, "markets": 2, "player_name": "Incumbent"},
        2: {"points": 15.0, "markets": 3, "player_name": "Alternative"},
    }

    rows, no_market = sunday.ranked_undecided(held, _two_wr_recommended(), market, {})

    assert no_market == set()
    row = rows[0]
    assert [c["player_name"] for c in row["candidates"]] == ["Alternative", "Incumbent"]
    assert row["market_best"]["player_name"] == "Alternative"
    assert row["friday_best"]["player_name"] == "Alternative"  # only other WR-eligible
    assert row["agrees"] is True


def test_ranked_undecided_flags_a_disagreement_between_market_and_projection():
    """Three candidates, so Friday's argmax-by-projection and the market's
    top price can name different players."""
    recommended = _two_wr_recommended()
    recommended["candidates"].append({"player_id": 3, "player_name": "Third"})
    recommended["eligibility"].append({"WR"})
    recommended["values"].append(11.0)  # beats Alternative's 10.0 on projection
    held = [{"slot": "WR", "player_name": "Incumbent", "reason": "Incumbent is COIN_FLIP"}]
    market = {
        1: {"points": 8.0, "markets": 2, "player_name": "Incumbent"},
        2: {"points": 15.0, "markets": 3, "player_name": "Alternative"},
        3: {"points": 9.0, "markets": 1, "player_name": "Third"},
    }

    rows, _ = sunday.ranked_undecided(held, recommended, market, {})
    row = rows[0]

    assert row["market_best"]["player_name"] == "Alternative"
    assert row["friday_best"]["player_name"] == "Third"
    assert row["agrees"] is False


def test_ranked_undecided_sorts_unpriced_candidates_last_and_names_them():
    """Never coerced to 0.0, which would rank them worst on apparent merit
    rather than on a missing quote."""
    held = [{"slot": "WR", "player_name": "Incumbent", "reason": "Incumbent is COIN_FLIP"}]
    market = {2: {"points": 15.0, "markets": 3, "player_name": "Alternative"}}

    rows, no_market = sunday.ranked_undecided(held, _two_wr_recommended(), market, {})

    assert no_market == {"Incumbent"}
    row = rows[0]
    assert [c["rank_source"] for c in row["candidates"]] == ["pre_lock", "espn"]
    assert row["candidates"][-1]["player_name"] == "Incumbent"
    assert row["candidates"][-1]["prop_points"] is None


def test_ranked_undecided_returns_nothing_when_no_slot_is_undecided():
    rows, no_market = sunday.ranked_undecided([], _two_wr_recommended(), {}, {})
    assert rows == []
    assert no_market == set()


def test_undecided_slots_returns_no_held_rows_when_the_solve_is_insufficient():
    recommended, held = sunday.undecided_slots(
        pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), _ALLOWED, {}, {}
    )
    assert recommended["insufficient"] is True
    assert held == []


# ---- overnight_out ---------------------------------------------------------

def _out_fixtures(baseline_status, current_status, current_practice=None):
    week_rosters = pd.DataFrame([
        _roster_row(1, "Starter", "RB", "RB", True),
        _roster_row(2, "Benchie", "RB", "Bench", False),
    ])
    starters = week_rosters[week_rosters["started"]]
    bench = week_rosters[~week_rosters["started"]]
    pool_df = pd.DataFrame([
        _pool_row(1, "Starter", "RB", ["RB", "RB/WR"], 12.0),
        _pool_row(2, "Benchie", "RB", ["RB", "RB/WR"], 9.0),
    ])
    snapshots = {
        date(2026, 9, 19): pd.DataFrame([_slim_row("s1", injury_status=baseline_status)]),
        _SUNDAY: pd.DataFrame([_slim_row("s1", injury_status=current_status,
                                         practice_participation=current_practice)]),
    }
    return week_rosters, starters, bench, pool_df, snapshots, _sleeper_map([(1, "s1")])


def test_overnight_out_pairs_a_newly_out_starter_with_a_bench_replacement():
    week_rosters, starters, bench, pool_df, snapshots, sleeper_df = _out_fixtures(None, "OUT")

    result = sunday.overnight_out(
        week_rosters, starters, bench, pool_df, _ALLOWED, {}, _SUNDAY,
        snapshots_by_date=snapshots, sleeper_df=sleeper_df,
    )

    assert [r["player_name"] for r in result["moved"]] == ["Starter"]
    assert result["moved"][0]["since"] == "overnight"
    assert result["moved"][0]["replacement"]["player_name"] == "Benchie"
    assert result["already_out"] == []


def test_overnight_out_keeps_a_starter_already_out_at_the_baseline_in_its_own_bucket():
    """The case the spec's "moved to OUT overnight" wording leaves out and
    this report cannot: an already-OUT starter is still a lineup you must not
    lock."""
    week_rosters, starters, bench, pool_df, snapshots, sleeper_df = _out_fixtures("OUT", "OUT")

    result = sunday.overnight_out(
        week_rosters, starters, bench, pool_df, _ALLOWED, {}, _SUNDAY,
        snapshots_by_date=snapshots, sleeper_df=sleeper_df,
    )

    assert result["moved"] == []
    assert [r["player_name"] for r in result["already_out"]] == ["Starter"]
    assert result["already_out"][0]["since"] == "already OUT at the baseline"
    assert result["already_out"][0]["replacement"]["player_name"] == "Benchie"


def test_overnight_out_ignores_a_worsening_move_short_of_out():
    """HIGH_RISK-but-not-OUT belongs to the undecided-slots section; a second
    table here would say it twice. (DOUBTFUL is no use as the fixture -- it
    sits in sleeper_signals.OUT_STATUSES and resolves to OUT; QUESTIONABLE
    with a DNP practice is the real short-of-OUT case.)"""
    week_rosters, starters, bench, pool_df, snapshots, sleeper_df = _out_fixtures(
        None, "QUESTIONABLE", current_practice="DNP"
    )

    result = sunday.overnight_out(
        week_rosters, starters, bench, pool_df, _ALLOWED, {}, _SUNDAY,
        snapshots_by_date=snapshots, sleeper_df=sleeper_df,
    )

    assert result["moved"] == []
    assert result["already_out"] == []


def test_overnight_out_gate_insufficient_returns_empty_buckets_not_an_all_clear():
    week_rosters, starters, bench, pool_df, _, sleeper_df = _out_fixtures(None, "OUT")

    result = sunday.overnight_out(
        week_rosters, starters, bench, pool_df, _ALLOWED, {}, _SUNDAY,
        snapshots_by_date={}, sleeper_df=sleeper_df,
    )

    assert result["gate"]["insufficient"] is True
    assert result["moved"] == [] and result["already_out"] == []


# ---- minutes_to_lock -------------------------------------------------------

def _games(rows):
    return pd.DataFrame([{"season": 2026, "week": 3, "game_type": "REG", **r} for r in rows])


def test_minutes_to_lock_reads_the_first_sunday_kickoff_from_nflverse(monkeypatch):
    games = _games([
        {"home_team": "KC", "away_team": "DEN", "weekday": "Sunday",
         "gameday": "2026-09-20", "gametime": "16:25"},
        {"home_team": "GB", "away_team": "MIN", "weekday": "Sunday",
         "gameday": "2026-09-20", "gametime": "13:00"},
    ])
    monkeypatch.setattr(sunday.schedule.nflverse_store, "load", lambda name: games)

    lock = sunday.minutes_to_lock(2026, 3, _RENDERED_AT)

    assert lock["source"] == "nflverse schedules"
    assert lock["kickoff_label"] == "13:00 ET"
    assert lock["minutes"] == 90


def test_minutes_to_lock_falls_back_to_the_spec_default_when_the_slate_cannot_be_read(monkeypatch):
    """Departs from saturday.bye_week_starters, which leaves the same
    exposure unguarded -- this value feeds the report's first body line, and
    a header that can take the whole render down is worse than one with a
    stated fallback."""
    def _raise(name):
        raise RuntimeError("nflverse schedules missing")

    monkeypatch.setattr(sunday.schedule.nflverse_store, "load", _raise)

    lock = sunday.minutes_to_lock(2026, 3, _RENDERED_AT)

    assert lock["source"] == "the spec's 13:00 ET default"
    assert "could not be loaded" in lock["reason"]
    assert lock["minutes"] == 90


def test_minutes_to_lock_no_sunday_game_falls_back_rather_than_raising(monkeypatch):
    monkeypatch.setattr(sunday.schedule.nflverse_store, "load", lambda name: _games([
        {"home_team": "KC", "away_team": "DEN", "weekday": "Monday",
         "gameday": "2026-09-21", "gametime": "20:15"},
    ]))

    lock = sunday.minutes_to_lock(2026, 3, _RENDERED_AT)

    assert lock["source"] == "the spec's 13:00 ET default"
    assert "no Sunday game" in lock["reason"]


def test_minutes_to_lock_after_kickoff_returns_a_negative_figure_not_none(monkeypatch):
    monkeypatch.setattr(sunday.schedule.nflverse_store, "load", lambda name: _games([
        {"home_team": "GB", "away_team": "MIN", "weekday": "Sunday",
         "gameday": "2026-09-20", "gametime": "13:00"},
    ]))
    after = pd.Timestamp("2026-09-20 13:30", tz="America/New_York").timestamp()

    lock = sunday.minutes_to_lock(2026, 3, after)

    assert lock["minutes"] == -30


# ---- render ----------------------------------------------------------------

def _stub_freshness(monkeypatch, odds_stale=False):
    monkeypatch.setattr(sunday, "freshness", lambda season=None: {
        "sleeper": (1_760_000_000, False), "nflverse": (1_760_000_000, False),
        "espn": (1_760_000_000, True), "odds": (1_760_000_000, odds_stale),
    })


def test_render_keeps_the_odds_freshness_line_and_adds_a_pre_lock_one(monkeypatch):
    """The deliberate mirror of saturday's
    test_render_freshness_header_has_no_odds_line. Saturday strips `odds:`
    because no odds job runs; Sunday keeps it and labels it, because
    loaders._odds_freshness reads slate_context, not pre_lock."""
    _stub_freshness(monkeypatch)
    text = _render(gate={"insufficient": False, "reason": None, "ran_at": _ran_at(),
                         "credits_spent": 21, "stale_flag": False})

    assert "- odds:" in text
    assert "- odds (`pre_lock`):" in text
    assert "`slate_context`" in text


def test_render_puts_the_generation_timestamp_above_the_freshness_block(monkeypatch):
    """Pins the spec's "prominently enough that a stale tab is obvious"
    structurally rather than by eyeball."""
    _stub_freshness(monkeypatch)
    text = _render()
    assert text.index("**Generated") < text.index("## Freshness")
    assert "90 minutes to the first Sunday kickoff" in text


def test_render_after_kickoff_says_the_report_is_history(monkeypatch):
    _stub_freshness(monkeypatch)
    text = _render(lock={**_LOCK, "minutes": -30})
    assert "has already passed" in text
    assert "history, not a decision" in text


def test_render_with_every_frame_empty_produces_complete_document(monkeypatch):
    _stub_freshness(monkeypatch)
    text = _render()

    assert "# Final lock -- 2026 week 3" in text
    assert "## Decisions due" in text
    assert "## Did `pre_lock` actually run" in text
    assert "## Slots still undecided as of this morning" in text
    assert "No slot is undecided" in text
    assert "## Starters whose tier reads `OUT`" in text
    assert "## The lineup you are locking" in text
    assert "## Pre-lock featured lines" in text
    assert "## Pre-lock props -- still-undecided slots only" in text
    assert "## What this report cannot see" in text
    assert sunday.INSUFFICIENT_DATA in text
    assert "Official inactives drop roughly 90 minutes before kickoff" in text


def test_render_banners_a_featured_capture_that_is_not_from_this_run(monkeypatch):
    _stub_freshness(monkeypatch)
    text = _render(
        gate={"insufficient": False, "reason": None, "ran_at": _ran_at(),
              "credits_spent": 21, "stale_flag": False},
        lines={"insufficient": False, "reason": None, "from_this_run": False,
               "current_at": "2026-09-18T14:08:00+00:00", "prior_at": None,
               "rows": [{"team": "MIN", "spread": -2.5, "total": 44.5, "implied": 23.5,
                         "spread_prior": None, "implied_prior": None, "implied_delta": None}]},
    )
    assert "**These rows are not from this morning's run**" in text


# ---- build(): the wiring, end to end ---------------------------------------

def test_build_assembles_the_document_and_names_what_it_could_not_verify(monkeypatch):
    """The only path that exercises build()'s own wiring -- every odds gate
    insufficient, which is exactly the shape a week-one Sunday takes before
    pre_lock has ever run, with one slot genuinely held open so the
    undecided path renders rather than short-circuiting.

    The at-risk player carries the *higher* projection on purpose:
    friday.held_open_slots holds a slot when the recommended starter is
    at-risk and the gap to the alternative is inside HOLD_OPEN_GAP, so the
    at-risk player has to be the one the solve picks."""
    exports = {
        "weekly-rosters": pd.DataFrame([
            _roster_row(1, "Hurt Starter", "RB", "RB", True),
            _roster_row(2, "Good Backup", "RB", "Bench", False),
        ]),
        "player-pool": pd.DataFrame([
            _pool_row(1, "Hurt Starter", "RB", "RB, RB/WR, Bench", 9.0),
            _pool_row(2, "Good Backup", "RB", "RB, RB/WR, Bench", 8.0),
        ]),
        "roster-slots": pd.DataFrame([
            {"slot": "RB", "count": 1}, {"slot": "Bench", "count": 2}, {"slot": "IR", "count": 1},
        ]),
    }
    monkeypatch.setattr(sunday, "latest_export", lambda name: exports.get(name, pd.DataFrame()))
    monkeypatch.setattr(
        sunday.availability, "read",
        lambda players_df, season, week: pd.DataFrame([
            {"player_id": 1, "tier": "COIN_FLIP"}, {"player_id": 2, "tier": "CLEAR"},
        ]),
    )
    monkeypatch.setattr(
        sunday.wednesday, "practice_signals",
        lambda players_df, today=None: pd.DataFrame(
            [{"player_id": pid, "practice_trajectory": "LP / LP / LP", "matched": True} for pid in (1, 2)]
        ),
    )
    monkeypatch.setattr(sunday, "pre_lock_read", lambda today=None: dict(_EMPTY_GATE))
    monkeypatch.setattr(sunday, "overnight_out", lambda *a, **k: dict(_EMPTY_OUT))
    monkeypatch.setattr(sunday, "minutes_to_lock", lambda season, week, rendered_at: dict(_LOCK))
    monkeypatch.setattr(sunday, "freshness", lambda season=None: {
        "sleeper": (None, True), "nflverse": (None, True), "espn": (None, True), "odds": (None, True),
    })
    monkeypatch.setattr(sunday, "espn_export_warning", lambda: None)

    text = sunday.build(2026, week=3, team_id=5)

    assert "# Final lock -- 2026 week 3" in text
    lineup = text.split("## The lineup you are locking")[1].split("## Pre-lock featured lines")[0]
    assert "Hurt Starter" in lineup

    # The at-risk starter's slot is held open, and with no pre_lock capture
    # its candidates rank on ESPN's projection -- named, never coerced.
    undecided = text.split("## Slots still undecided")[1].split("## Starters whose tier")[0]
    assert "### RB -- Hurt Starter" in undecided
    assert "ranked on ESPN's projection alone" in undecided

    # Every odds gate was insufficient, so the footer must say so rather than
    # the market sections rendering as if they had data.
    assert "`pre_lock` could not be verified" in text
    assert "absence of evidence, not an all-clear" in text
    assert "The sleeper feed is stale as of this report's generation." in text
    assert "Ranked on ESPN's `week_projected` rather than the `pre_lock` consensus" in text


def test_inactives_footer_matches_saturdays_wording():
    """The two reports must not drift into different claims about the one
    thing neither can see."""
    from espn_ff.report import saturday as saturday_report

    fragment = "Official inactives drop roughly 90 minutes before kickoff and appear in no feed this pipeline"
    assert any(fragment in note for note in sunday.FOOTER_NOTES)
    assert any(fragment in note for note in saturday_report.FOOTER_NOTES)
