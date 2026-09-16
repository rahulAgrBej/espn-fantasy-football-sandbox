"""Wednesday's availability watchlist -- the mixed-vocabulary tier
normalizer, the watchlist filter and its replacement pairing, the
practice/depth-chart signal join, and the empty-everything render.
Fixtures only, no network, no disk."""

import datetime as dt

import pandas as pd
import pytest

from espn_ff.report import wednesday


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
        wednesday.FOOTER_NOTES, window=None, rendered_at=1_760_000_000,
    )
    assert "# Availability watchlist -- 2026 week 2" in text
    assert "## Watchlist" in text and "## Depth chart moves" in text
    assert "## What this report cannot see" in text
    assert "insufficient data" in text
