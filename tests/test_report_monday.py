"""Monday report assembly logic -- margin math, the unrefreshed-matchups
path, slot-eligibility intersection, and the no-legal-swap message.
Fixtures only, no network, no disk."""

import pandas as pd

from espn_ff.report import monday


def _matchups_df(our_live, their_live, team_id=5, opponent_id=7):
    return pd.DataFrame(
        [
            {"week": 2, "team_id": team_id, "points_live": our_live, "points_final": 0.0,
             "opponent_id": opponent_id, "opponent_name": "Opponent Team", "winner": "UNDECIDED"},
            {"week": 2, "team_id": opponent_id, "points_live": their_live, "points_final": 0.0,
             "opponent_id": team_id, "opponent_name": "Our Team", "winner": "UNDECIDED"},
        ]
    )


def test_live_margin_math_when_we_are_behind():
    matchups = _matchups_df(our_live=80.0, their_live=95.0)
    result = monday.live_margin(matchups, week=2, team_id=5)
    assert result["insufficient"] is False
    assert result["margin"] == 15.0


def test_live_margin_math_when_we_are_ahead():
    matchups = _matchups_df(our_live=100.0, their_live=60.0)
    result = monday.live_margin(matchups, week=2, team_id=5)
    assert result["margin"] == -40.0


def test_live_margin_flags_unrefreshed_matchups_as_insufficient():
    """Both sides reading 0.0 is the signature of a 09:08 ESPN refresh that
    has not landed -- it must render as insufficient data, never as a real
    0-0 tie."""
    matchups = _matchups_df(our_live=0.0, their_live=0.0)
    result = monday.live_margin(matchups, week=2, team_id=5)
    assert result["insufficient"] is True
    # opponent identity is still known even though the score can't be trusted
    assert result["opponent_id"] == 7


def test_live_margin_no_matchup_row_is_insufficient():
    result = monday.live_margin(pd.DataFrame(columns=["week", "team_id"]), week=2, team_id=5)
    assert result["insufficient"] is True
    assert result["opponent_id"] is None


def test_league_slots_excludes_flex_and_op():
    roster_slots = pd.DataFrame(
        [
            {"slot": "QB", "count": 1},
            {"slot": "RB", "count": 2},
            {"slot": "D/ST", "count": 1},
        ]
    )
    assert monday.league_slots(roster_slots) == {"QB", "RB", "D/ST"}


def test_eligible_slots_for_intersects_with_league_slots():
    pool_df = pd.DataFrame(
        [{"player_id": 1, "eligible_slots": "RB,RB/WR,FLEX,OP,Bench,IR"}]
    )
    allowed = {"RB", "RB/WR", "Bench", "IR"}  # this league's actual slots -- no FLEX/OP
    result = monday.eligible_slots_for(1, pool_df, allowed)
    assert result == {"RB", "RB/WR", "Bench", "IR"}
    assert "FLEX" not in result
    assert "OP" not in result


def _starter(player_id=1, lineup_slot="RB"):
    return pd.Series({"player_id": player_id, "player_name": "Our Starter", "lineup_slot": lineup_slot})


def test_find_alternatives_no_legal_swap_when_nobody_is_on_either_team():
    bench_df = pd.DataFrame(
        [{"player_id": 2, "player_name": "Bench Guy", "pro_team": "PIT", "lineup_slot": "Bench", "projected": 10.0}]
    )
    free_agents_df = pd.DataFrame(
        [{"player_id": 3, "player_name": "FA Guy", "pro_team": "CAR", "week_projected": 5.0}]
    )
    pool_df = pd.DataFrame(
        [
            {"player_id": 2, "eligible_slots": "RB,Bench,IR"},
            {"player_id": 3, "eligible_slots": "RB,Bench,IR"},
        ]
    )
    result = monday.find_alternatives(
        _starter(), bench_df, free_agents_df, pool_df, allowed_slots={"RB", "Bench", "IR"},
        monday_teams={"NYG", "LAR"},
    )
    assert result["no_swap"] is True
    assert result["bench"].empty
    assert result["free_agents"].empty


def test_find_alternatives_finds_a_bench_candidate_on_the_monday_teams():
    bench_df = pd.DataFrame(
        [
            {"player_id": 2, "player_name": "Bench On Team", "pro_team": "NYG", "lineup_slot": "Bench", "projected": 12.0},
            {"player_id": 4, "player_name": "Bench Off Team", "pro_team": "PIT", "lineup_slot": "Bench", "projected": 20.0},
        ]
    )
    free_agents_df = pd.DataFrame(columns=["player_id", "player_name", "pro_team", "week_projected"])
    pool_df = pd.DataFrame(
        [
            {"player_id": 2, "eligible_slots": "RB,Bench,IR"},
            {"player_id": 4, "eligible_slots": "RB,Bench,IR"},
        ]
    )
    result = monday.find_alternatives(
        _starter(), bench_df, free_agents_df, pool_df, allowed_slots={"RB", "Bench", "IR"},
        monday_teams={"NYG", "LAR"},
    )
    assert result["no_swap"] is False
    assert list(result["bench"]["player_name"]) == ["Bench On Team"]


def test_find_alternatives_excludes_ir_slot_bench_players():
    bench_df = pd.DataFrame(
        [{"player_id": 2, "player_name": "IR Guy", "pro_team": "NYG", "lineup_slot": "IR", "projected": 15.0}]
    )
    free_agents_df = pd.DataFrame(columns=["player_id", "player_name", "pro_team", "week_projected"])
    pool_df = pd.DataFrame([{"player_id": 2, "eligible_slots": "RB,Bench,IR"}])
    result = monday.find_alternatives(
        _starter(), bench_df, free_agents_df, pool_df, allowed_slots={"RB", "Bench", "IR"},
        monday_teams={"NYG", "LAR"},
    )
    assert result["no_swap"] is True


def test_find_alternatives_excludes_ineligible_slot():
    """A bench player on a Monday-game team who isn't eligible for the
    starter's slot must not be offered as a swap."""
    bench_df = pd.DataFrame(
        [{"player_id": 2, "player_name": "Wrong Slot Guy", "pro_team": "NYG", "lineup_slot": "Bench", "projected": 12.0}]
    )
    free_agents_df = pd.DataFrame(columns=["player_id", "player_name", "pro_team", "week_projected"])
    pool_df = pd.DataFrame([{"player_id": 2, "eligible_slots": "TE,Bench,IR"}])  # not RB-eligible
    result = monday.find_alternatives(
        _starter(lineup_slot="RB"), bench_df, free_agents_df, pool_df,
        allowed_slots={"RB", "TE", "Bench", "IR"}, monday_teams={"NYG", "LAR"},
    )
    assert result["no_swap"] is True


def test_players_in_game_tags_both_sides():
    rosters = pd.DataFrame(
        [
            {"week": 2, "team_id": 5, "player_id": 1, "player_name": "Our Guy", "position": "RB",
             "pro_team": "NYG", "started": True},
            {"week": 2, "team_id": 7, "player_id": 2, "player_name": "Their Guy", "position": "WR",
             "pro_team": "LAR", "started": True},
            {"week": 2, "team_id": 5, "player_id": 3, "player_name": "Bench Guy", "position": "WR",
             "pro_team": "NYG", "started": False},
            {"week": 2, "team_id": 5, "player_id": 4, "player_name": "Not Playing Tonight", "position": "TE",
             "pro_team": "KC", "started": True},
        ]
    )
    result = monday.players_in_game(rosters, week=2, team_id=5, opponent_id=7, monday_teams={"NYG", "LAR"})
    assert set(result["player_id"]) == {1, 2}
    assert dict(zip(result["player_id"], result["side"])) == {1: "ours", 2: "opponent"}


def test_players_in_game_empty_when_no_monday_teams():
    rosters = pd.DataFrame([{"week": 2, "team_id": 5, "player_id": 1, "started": True, "pro_team": "NYG"}])
    result = monday.players_in_game(rosters, week=2, team_id=5, opponent_id=7, monday_teams=set())
    assert result.empty
