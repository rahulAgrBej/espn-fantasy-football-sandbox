"""Wednesday's availability watchlist -- the mixed-vocabulary tier
normalizer, the watchlist filter and its replacement pairing, the
practice/depth-chart signal join, and the empty-everything render.
Fixtures only, no network, no disk."""

import datetime as dt

import pandas as pd
import pytest

from espn_ff.report import wednesday

import payload_helpers


# ---- fixture builders (same shapes as tests/test_report_waivers.py) ----

def _roster_row(player_id, player_name, position, lineup_slot, started,
                projected=None, week=2, team_id=5, injury_status="ACTIVE", pro_team="KC"):
    return {
        "season": 2026, "week": week, "team_id": team_id, "team_name": "Us",
        "player_id": player_id, "player_name": player_name, "position": position,
        "pro_team": pro_team, "lineup_slot_id": 0, "lineup_slot": lineup_slot,
        "started": started, "injury_status": injury_status, "acquisition_type": "DRAFT",
        "points": None, "projected": projected,
    }


def _pool_row(player_id, player_name, position, eligible_slots, week_projected, week=2):
    return {
        "player_id": player_id, "player_name": player_name, "position": position,
        "pro_team": "KC", "eligible_slots": eligible_slots, "week": week,
        "week_projected": week_projected, "percent_owned": 50.0,
        "season_points": 0.0, "season_projected": 0.0,
    }


_INSUFFICIENT_OUTCOMES = {
    "insufficient": True, "reason": "no transactions export on disk",
    "claimed_by_us": [], "claimed_by_others": [], "newly_available": [],
    "pending_count": 0, "excluded_count": 0, "unresolved_ids": set(),
}


_ROSTER_SLOTS = pd.DataFrame([
    {"slot": "QB", "count": 1}, {"slot": "RB", "count": 2}, {"slot": "WR", "count": 2},
    {"slot": "RB/WR", "count": 1}, {"slot": "TE", "count": 1}, {"slot": "D/ST", "count": 1},
    {"slot": "K", "count": 1}, {"slot": "Bench", "count": 6}, {"slot": "IR", "count": 1},
])
_ALLOWED = {"QB", "RB", "WR", "RB/WR", "TE", "D/ST", "K", "Bench", "IR"}


# ---- the normalizer: the trap this module exists to avoid ---------------

@pytest.mark.parametrize("tier", ["OUT", "HIGH_RISK", "COIN_FLIP",
                                   "Out", "Doubtful", "Questionable", "IR"])
def test_at_risk_across_all_three_vocabularies(tier):
    """nflverse hands back "Out"/"Doubtful"/"Questionable", Sleeper hands
    back OUT/HIGH_RISK/COIN_FLIP, ESPN hands back its own -- a watchlist
    filtering on the Sleeper names alone would drop every nflverse row."""
    assert wednesday.is_at_risk(tier)


@pytest.mark.parametrize("tier", ["CLEAR", "LIKELY_PLAYS", "ACTIVE", "UNKNOWN",
                                   None, float("nan"), "insufficient data"])
def test_not_at_risk(tier):
    """UNKNOWN and insufficient data are explicitly NOT at-risk: neither is
    a designation, and promoting either would fill the watchlist with rows
    whose only content is a feed gap."""
    assert not wednesday.is_at_risk(tier)


# ---- the watchlist ------------------------------------------------------

def _starters_and_bench():
    rosters = pd.DataFrame([
        _roster_row(1, "Hurt Starter", "RB", "RB", True, projected=14.0),
        _roster_row(2, "Fine Starter", "WR", "WR", True, projected=11.0),
        _roster_row(3, "Good Backup", "RB", "Bench", False, projected=9.0),
        _roster_row(4, "Weak Backup", "RB", "Bench", False, projected=4.0),
        _roster_row(5, "Stashed", "RB", "IR", False, projected=20.0),
    ])
    pool = pd.DataFrame([
        _pool_row(1, "Hurt Starter", "RB", "RB, RB/WR, Bench", 14.0),
        _pool_row(2, "Fine Starter", "WR", "WR, RB/WR, Bench", 11.0),
        _pool_row(3, "Good Backup", "RB", "RB, RB/WR, Bench", 9.0),
        _pool_row(4, "Weak Backup", "RB", "RB, RB/WR, Bench", 4.0),
        _pool_row(5, "Stashed", "RB", "RB, RB/WR, Bench", 20.0),
    ])
    return rosters, pool


def _avail(tiers):
    return pd.DataFrame([
        {"player_id": pid, "player_name": name, "tier": tier,
         "espn_injury_status": "ACTIVE", "sleeper_tier": tier,
         "nflverse_report_status": None}
        for pid, name, tier in tiers
    ])


def test_only_at_risk_starters_appear_paired_with_best_legal_bench():
    rosters, pool = _starters_and_bench()
    starters = wednesday.our_starters(rosters, week=2, team_id=5)
    bench = rosters[(~rosters["started"]) & (rosters["lineup_slot"] != "IR")]
    avail = _avail([(1, "Hurt Starter", "HIGH_RISK"), (2, "Fine Starter", "CLEAR"),
                    (3, "Good Backup", "CLEAR"), (4, "Weak Backup", "CLEAR")])

    rows, _, _ = wednesday.watchlist(starters, bench, avail, pd.DataFrame(), pool, _ALLOWED)

    assert [r["player_name"] for r in rows] == ["Hurt Starter"]
    assert rows[0]["replacement"]["player_name"] == "Good Backup"   # 9.0 beats 4.0
    assert rows[0]["projection"] == 14.0                            # points at risk, not the delta


def test_ir_player_is_never_a_replacement():
    """`started == False` alone includes IR rows; tuesday._bench's explicit
    IR filter is why the 20.0-projected stashed player cannot be offered."""
    rosters, pool = _starters_and_bench()
    starters = wednesday.our_starters(rosters, week=2, team_id=5)
    bench = rosters[(~rosters["started"]) & (rosters["lineup_slot"] != "IR")]
    avail = _avail([(1, "Hurt Starter", "OUT")])

    rows, _, _ = wednesday.watchlist(starters, bench, avail, pd.DataFrame(), pool, _ALLOWED)
    assert rows[0]["replacement"]["player_name"] != "Stashed"


def test_no_legal_swap_renders_rather_than_dropping_the_row():
    rosters = pd.DataFrame([
        _roster_row(1, "Hurt Kicker", "K", "K", True, projected=8.0),
        _roster_row(3, "Good Backup", "RB", "Bench", False, projected=9.0),
    ])
    pool = pd.DataFrame([
        _pool_row(1, "Hurt Kicker", "K", "K, Bench", 8.0),
        _pool_row(3, "Good Backup", "RB", "RB, RB/WR, Bench", 9.0),
    ])
    starters = wednesday.our_starters(rosters, week=2, team_id=5)
    bench = rosters[~rosters["started"]]
    avail = _avail([(1, "Hurt Kicker", "Doubtful"), (3, "Good Backup", "CLEAR")])

    rows, _, _ = wednesday.watchlist(starters, bench, avail, pd.DataFrame(), pool, _ALLOWED)
    assert len(rows) == 1 and rows[0]["replacement"] is None
    text = wednesday.render(2026, 2, 5, rows, pd.DataFrame(), avail, starters, [],
                            _INSUFFICIENT_OUTCOMES, {},
                            wednesday.FOOTER_NOTES, rendered_at=1_760_000_000)
    assert "no legal swap" in text


# ---- practice signals ---------------------------------------------------

def test_practice_trajectory_reads_wed_only_on_a_wednesday():
    """`Wed / -- / --` is the correct Wednesday output, not a missing feed."""
    today = dt.date(2026, 9, 16)   # a Wednesday
    slim = pd.DataFrame([{
        "sleeper_id": "s1", "espn_player_id": 1, "practice_participation": "DNP",
        "depth_chart_position": "RB", "depth_chart_order": 1,
    }])
    signals = wednesday.practice_signals(
        pd.DataFrame([{"player_id": 1, "player_name": "Hurt Starter"}]),
        today=today, snapshots_by_date={today: slim}, sleeper_df=slim,
    )
    assert signals.iloc[0]["practice_trajectory"] == "DNP / — / —"
    assert signals.iloc[0]["matched"]


def test_unmatched_player_renders_blank_not_clear():
    signals = wednesday.practice_signals(
        pd.DataFrame([{"player_id": 99, "player_name": "Ghost"}]),
        today=dt.date(2026, 9, 16), snapshots_by_date={}, sleeper_df=pd.DataFrame(),
    )
    row = signals.iloc[0]
    assert not row["matched"] and row["practice_trajectory"] is None


# ---- the no-data render: the smoke check ------------------------------

def test_renders_with_no_data_at_all(monkeypatch):
    """Every input empty -- the shape a first scheduled run hits before any
    export has been restored. It must render every section as insufficient
    data and never raise."""
    monkeypatch.setattr(wednesday, "freshness", lambda season=None: {
        "sleeper": (None, True), "nflverse": (None, True),
        "espn": (None, True), "odds": (None, True),
    })
    text = wednesday.render(
        2026, 2, 5, [], pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), [],
        _INSUFFICIENT_OUTCOMES, {},
        wednesday.FOOTER_NOTES, window=None, rendered_at=1_760_000_000,
    )
    assert "# Availability watchlist -- 2026 week 2" in text
    assert "## Watchlist" in text and "## Depth chart moves" in text
    assert "## Waiver outcomes" in text
    assert "## What this report cannot see" in text
    assert "insufficient data" in text


# ---- _alternates ---------------------------------------------------------

def test_alternates_ranks_by_projection_same_slot_only():
    lost_player = {"player_id": 1, "player_name": "Lost TE", "position": "TE"}
    pool_df = pd.DataFrame([
        _pool_row(1, "Lost TE", "TE", "TE, Bench", 8.0),
        _pool_row(10, "FA TE Hi", "TE", "TE, Bench", 9.0),
        _pool_row(11, "FA TE Lo", "TE", "TE, Bench", 3.0),
        _pool_row(12, "FA RB", "RB", "RB, RB/WR, Bench", 15.0),
    ])
    free_agents_df = pool_df[pool_df["player_id"].isin([10, 11, 12])].reset_index(drop=True)

    alts, fallback_names, nan_names = wednesday._alternates(lost_player, free_agents_df, pool_df, _ALLOWED)

    assert [a["player_name"] for a in alts] == ["FA TE Hi", "FA TE Lo"]
    assert fallback_names == set()
    assert nan_names == set()


def test_alternates_empty_when_no_eligible_candidate():
    lost_player = {"player_id": 1, "player_name": "Lost TE", "position": "TE"}
    pool_df = pd.DataFrame([
        _pool_row(1, "Lost TE", "TE", "TE, Bench", 8.0),
        _pool_row(12, "FA RB", "RB", "RB, RB/WR, Bench", 15.0),
    ])
    free_agents_df = pool_df[pool_df["player_id"] == 12].reset_index(drop=True)

    alts, _, _ = wednesday._alternates(lost_player, free_agents_df, pool_df, _ALLOWED)
    assert alts == []


def test_alternates_respects_per_slot():
    lost_player = {"player_id": 1, "player_name": "Lost TE", "position": "TE"}
    rows = [_pool_row(1, "Lost TE", "TE", "TE, Bench", 8.0)]
    rows += [_pool_row(10 + i, f"FA TE {i}", "TE", "TE, Bench", float(i)) for i in range(5)]
    pool_df = pd.DataFrame(rows)
    free_agents_df = pool_df[pool_df["player_id"] != 1].reset_index(drop=True)

    alts, _, _ = wednesday._alternates(lost_player, free_agents_df, pool_df, _ALLOWED, per_slot=3)
    assert len(alts) == 3
    assert [a["player_name"] for a in alts] == ["FA TE 4", "FA TE 3", "FA TE 2"]


def test_alternates_tracks_fallback_and_nan_names():
    # Ghost TE has no pool row at all -- eligibility falls back to the position map.
    lost_player = {"player_id": 999, "player_name": "Ghost TE", "position": "TE"}
    pool_df = pd.DataFrame([
        _pool_row(10, "FA TE NaN", "TE", "TE, Bench", float("nan")),
        _pool_row(11, "FA TE Valid", "TE", "TE, Bench", 5.0),
    ])
    free_agents_df = pool_df.copy()

    alts, fallback_names, nan_names = wednesday._alternates(lost_player, free_agents_df, pool_df, _ALLOWED)

    assert "Ghost TE" in fallback_names
    assert "FA TE NaN" in nan_names
    assert [a["player_name"] for a in alts] == ["FA TE Valid"]


# ---- render: waiver outcomes ----------------------------------------------

def _outcomes(claimed_by_us=None, claimed_by_others=None, newly_available=None, pending_count=0):
    return {
        "insufficient": False,
        "claimed_by_us": claimed_by_us or [],
        "claimed_by_others": claimed_by_others or [],
        "newly_available": newly_available or [],
        "pending_count": pending_count, "excluded_count": 0, "unresolved_ids": set(),
    }


def test_render_claimed_by_others_with_alternates_shows_subheading_and_mini_table():
    outcomes = _outcomes(claimed_by_others=[{
        "player_id": 5, "player_name": "Rival Add", "position": "RB", "pro_team": "SEA",
        "acting_team": "Team X", "acting_team_id": 3, "transaction_id": 100,
        "bid_amount": 0, "scoring_period": 2, "proposed_date": "2026-09-16",
    }])
    alternates_by_player = {5: [{"player_name": "Alt Guy", "position": "RB", "pro_team": "DAL", "week_projected": 9.0}]}

    text = wednesday.render(2026, 2, 5, [], pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), [],
                            outcomes, alternates_by_player, wednesday.FOOTER_NOTES, rendered_at=1_760_000_000)

    assert "#### Rival Add (RB) -- claimed by Team X" in text
    assert "Alt Guy" in text


def test_render_claimed_by_others_with_no_alternates_shows_explicit_message():
    outcomes = _outcomes(claimed_by_others=[{
        "player_id": 5, "player_name": "Rival Add", "position": "RB", "pro_team": "SEA",
        "acting_team": "Team X", "acting_team_id": 3, "transaction_id": 100,
        "bid_amount": 0, "scoring_period": 2, "proposed_date": "2026-09-16",
    }])

    text = wednesday.render(2026, 2, 5, [], pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), [],
                            outcomes, {}, wednesday.FOOTER_NOTES, rendered_at=1_760_000_000)

    assert "No same-slot free-agent alternate found." in text


def test_gate_failure_render_states_the_gap_and_drops_the_false_pull_claim():
    """The report must not assert a pull that did not happen. Before the
    gate, this line was unconditional and claimed 'this morning's ESPN pull
    is the first settled read' on a render whose payload predated the waiver
    run entirely (Observed 2026-09-16)."""
    outcomes = {
        **_INSUFFICIENT_OUTCOMES,
        "reason": "the ESPN transactions payload was fetched Tue 2026-09-15 14:10 ET, "
                  "before this week's waiver run",
    }
    text = wednesday.render(2026, 2, 5, [], pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), [],
                            outcomes, {}, wednesday.FOOTER_NOTES, rendered_at=1_760_000_000)

    assert "this morning's ESPN pull is the first settled read" not in text
    assert "before this week's waiver run" in text
    assert wednesday.INSUFFICIENT_DATA in text
    # and never a confident zero
    assert "0 claimed by us, 0 claimed by other teams" not in text


def test_gate_pass_keeps_the_settled_read_prose():
    outcomes = _outcomes()
    text = wednesday.render(2026, 2, 5, [], pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), [],
                            outcomes, {}, wednesday.FOOTER_NOTES, rendered_at=1_760_000_000)
    assert "this morning's ESPN pull is the first settled read" in text


def test_render_claimed_by_us_and_newly_available_tables_render():
    outcomes = _outcomes(
        claimed_by_us=[{
            "player_name": "Our Add", "position": "WR", "pro_team": "KC",
            "bid_amount": 0, "scoring_period": 2, "proposed_date": "2026-09-16",
        }],
        newly_available=[{
            "player_name": "Dropped Guy", "position": "RB", "pro_team": "NYJ",
            "dropped_by_team": "Team Y", "scoring_period": 2, "proposed_date": "2026-09-16",
        }],
    )

    text = wednesday.render(2026, 2, 5, [], pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), [],
                            outcomes, {}, wednesday.FOOTER_NOTES, rendered_at=1_760_000_000)

    assert "Our Add" in text
    assert "Dropped Guy" in text


# ---- payload: the JSON twin ---------------------------------------------

def _populated_args():
    """One argument set exercising every section kind this report has: a
    watchlist with and without a replacement, a full waiver-outcomes block
    with a `####` per-player group, depth-chart moves, and a practice table."""
    rosters = pd.DataFrame([
        _roster_row(1, "Hurt Starter", "RB", "RB", True, projected=12.0),
        _roster_row(3, "Good Backup", "RB", "Bench", False, projected=9.0),
    ])
    pool = pd.DataFrame([
        _pool_row(1, "Hurt Starter", "RB", "RB, RB/WR, Bench", 12.0),
        _pool_row(3, "Good Backup", "RB", "RB, RB/WR, Bench", 9.0),
    ])
    starters = wednesday.our_starters(rosters, week=2, team_id=5)
    bench = rosters[~rosters["started"]]
    avail = _avail([(1, "Hurt Starter", "Doubtful"), (3, "Good Backup", "CLEAR")])
    # No player_name: practice_signals does not emit one, which is why
    # depth_chart_moves takes roster_names separately. Adding it here would
    # collide with avail_df's on the practice-report merge.
    signals = pd.DataFrame([
        {"player_id": 1, "practice_participation": "DNP",
         "practice_trajectory": "DNP / — / —", "depth_chart_order": 1.0,
         "depth_chart_order_prev": 2.0, "improved": True, "promoted": False,
         "days_used": 3, "matched": True},
    ])
    watch_rows, _, _ = wednesday.watchlist(starters, bench, avail, signals, pool, _ALLOWED)
    outcomes = _outcomes(
        claimed_by_us=[{
            "player_name": "Our Add", "position": "WR", "pro_team": "KC",
            "bid_amount": 3, "scoring_period": 2, "proposed_date": "2026-09-16",
        }],
        claimed_by_others=[{
            "player_id": 5, "player_name": "Rival Add", "position": "RB", "pro_team": "SEA",
            "acting_team": "Team X", "acting_team_id": 3, "transaction_id": 100,
            "bid_amount": 0, "scoring_period": 2, "proposed_date": "2026-09-16",
        }],
        newly_available=[{
            "player_name": "Dropped Guy", "position": "RB", "pro_team": "NYJ",
            "dropped_by_team": "Team Y", "scoring_period": 2, "proposed_date": "2026-09-16",
        }],
        pending_count=2,
    )
    alternates = {5: [{"player_name": "Alt Guy", "position": "RB", "pro_team": "DAL",
                       "week_projected": 9.0}]}
    moves = [{"player_name": "Hurt Starter", "depth_chart_order": 1.0, "improved": True,
              "promoted": False, "days_used": 3}]
    return (2026, 2, 5, watch_rows, signals, avail, starters, moves,
            outcomes, alternates, wednesday.FOOTER_NOTES)


@pytest.mark.parametrize("args_name", ["empty", "populated"])
def test_payload_names_the_same_sections_as_the_markdown(monkeypatch, args_name):
    monkeypatch.setattr(wednesday, "freshness", lambda season=None: {
        "sleeper": (None, True), "nflverse": (None, True),
        "espn": (None, True), "odds": (None, True),
    })
    args = (
        (2026, 2, 5, [], pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), [],
         _INSUFFICIENT_OUTCOMES, {}, wednesday.FOOTER_NOTES)
        if args_name == "empty" else _populated_args()
    )
    kwargs = {"window": None, "rendered_at": 1_760_000_000}

    text = wednesday.render(*args, **kwargs)
    _, sections = wednesday.payload(*args, **kwargs)

    payload_helpers.assert_payload_matches_markdown(text, sections)
    payload_helpers.assert_no_display_strings(sections)


def test_payload_is_json_serializable(monkeypatch):
    monkeypatch.setattr(wednesday, "freshness", lambda season=None: {
        "sleeper": (None, True), "nflverse": (None, True),
        "espn": (None, True), "odds": (None, True),
    })
    header, sections = wednesday.payload(
        *_populated_args(), window=None, rendered_at=1_760_000_000
    )
    payload_helpers.assert_json_serializable(header, sections)


def test_payload_carries_a_missing_trajectory_as_null_not_dashes(monkeypatch):
    """render prints `-- / -- / --` for an absent trajectory. The JSON must
    say null: a consumer handed the dashes would display them as data."""
    monkeypatch.setattr(wednesday, "freshness", lambda season=None: {"sleeper": (None, True)})
    rosters = pd.DataFrame([
        _roster_row(1, "Hurt Starter", "RB", "RB", True, projected=12.0),
    ])
    pool = pd.DataFrame([_pool_row(1, "Hurt Starter", "RB", "RB, RB/WR, Bench", 12.0)])
    starters = wednesday.our_starters(rosters, week=2, team_id=5)
    avail = _avail([(1, "Hurt Starter", "Doubtful")])
    watch_rows, _, _ = wednesday.watchlist(
        starters, rosters[~rosters["started"]], avail, pd.DataFrame(), pool, _ALLOWED
    )
    _, sections = wednesday.payload(
        2026, 2, 5, watch_rows, pd.DataFrame(), avail, starters, [],
        _INSUFFICIENT_OUTCOMES, {}, wednesday.FOOTER_NOTES, rendered_at=1_760_000_000,
    )
    watchlist_section = next(s for s in sections if s["id"] == "watchlist")
    rows = watchlist_section["blocks"][0]["rows"]
    assert rows[0]["practice_trajectory"] is None
    assert rows[0]["replacement_name"] is None


def test_payload_counts_survive_outside_the_prose(monkeypatch):
    """The waiver-outcome counts exist in the markdown only inside an English
    sentence. `data` is what stops the JSON inheriting that."""
    monkeypatch.setattr(wednesday, "freshness", lambda season=None: {"sleeper": (None, True)})
    _, sections = wednesday.payload(*_populated_args(), rendered_at=1_760_000_000)
    outcomes_section = next(s for s in sections if s["id"] == "waiver-outcomes")
    assert outcomes_section["data"]["claimed_by_us_count"] == 1
    assert outcomes_section["data"]["pending_count"] == 2
