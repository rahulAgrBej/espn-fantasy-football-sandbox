"""Monday-game selection -- fixtures only, no network."""

import pandas as pd

from espn_ff.report import schedule


def _games(rows):
    return pd.DataFrame(rows)


def test_remaining_games_selects_only_the_requested_weekday(monkeypatch):
    games = _games(
        [
            {"season": 2026, "week": 2, "game_type": "REG", "home_team": "LA", "away_team": "NYG",
             "weekday": "Monday", "gameday": "2026-09-21", "gametime": "20:15"},
            {"season": 2026, "week": 2, "game_type": "REG", "home_team": "KC", "away_team": "DEN",
             "weekday": "Sunday", "gameday": "2026-09-20", "gametime": "13:00"},
        ]
    )
    monkeypatch.setattr(schedule.nflverse_store, "load", lambda name: games)

    result = schedule.remaining_games(2026, 2)
    assert len(result) == 1
    assert result.iloc[0]["gametime"] == "20:15"


def test_remaining_games_normalizes_team_aliases(monkeypatch):
    """nflverse's LA/WAS/JAC must come out as ESPN's LAR/WSH/JAX so a later
    join against weekly-rosters.csv's pro_team lines up."""
    games = _games(
        [
            {"season": 2026, "week": 2, "game_type": "REG", "home_team": "LA", "away_team": "NYG",
             "weekday": "Monday", "gameday": "2026-09-21", "gametime": "20:15"},
        ]
    )
    monkeypatch.setattr(schedule.nflverse_store, "load", lambda name: games)

    result = schedule.remaining_games(2026, 2)
    assert result.iloc[0]["home_team"] == "LAR"
    assert result.iloc[0]["away_team"] == "NYG"


def test_remaining_games_returns_empty_frame_for_a_bye_week(monkeypatch):
    """Week 18 (or any week with no Monday game) must not raise and must
    not be assumed to have exactly one row."""
    games = _games(
        [
            {"season": 2026, "week": 18, "game_type": "REG", "home_team": "GB", "away_team": "CHI",
             "weekday": "Sunday", "gameday": "2027-01-03", "gametime": "13:00"},
        ]
    )
    monkeypatch.setattr(schedule.nflverse_store, "load", lambda name: games)

    result = schedule.remaining_games(2026, 18)
    assert result.empty


def test_remaining_games_handles_more_than_one_monday_game(monkeypatch):
    """2026 happens to have exactly one Monday game per week, but the
    function must not assume that holds in general."""
    games = _games(
        [
            {"season": 2026, "week": 3, "game_type": "REG", "home_team": "CHI", "away_team": "PHI",
             "weekday": "Monday", "gameday": "2026-09-28", "gametime": "20:15"},
            {"season": 2026, "week": 3, "game_type": "REG", "home_team": "WAS", "away_team": "DAL",
             "weekday": "Monday", "gameday": "2026-09-28", "gametime": "19:00"},
        ]
    )
    monkeypatch.setattr(schedule.nflverse_store, "load", lambda name: games)

    result = schedule.remaining_games(2026, 3)
    assert len(result) == 2
    assert set(result["home_team"]) == {"CHI", "WSH"}


def test_teams_in_returns_home_and_away_normalized():
    games = _games([{"home_team": "LAR", "away_team": "NYG"}])
    assert schedule.teams_in(games) == {"LAR", "NYG"}


def test_teams_in_empty_frame_returns_empty_set():
    assert schedule.teams_in(pd.DataFrame()) == set()
