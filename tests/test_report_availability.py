"""The three-source availability read -- fixtures only, no network."""

import datetime as dt

import pandas as pd
import pytest

from espn_ff.report import availability


def _players_df():
    return pd.DataFrame(
        [
            {"player_id": 101, "player_name": "Agreeing Guy", "injury_status": "Questionable"},
            {"player_id": 102, "player_name": "Disagreeing Guy", "injury_status": "ACTIVE"},
            {"player_id": 103, "player_name": "No Designation Guy", "injury_status": "ACTIVE"},
        ]
    )


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Wires config.SLEEPER_ID_MAP / config.NFLVERSE_XWALK to tmp files and
    the Sleeper slim-snapshot lookup to an in-memory frame, so `read()` can
    be exercised end to end without touching real disk state."""
    id_map_path = tmp_path / "player_id_map.csv"
    xwalk_path = tmp_path / "player_xwalk.csv"
    monkeypatch.setattr(availability.config, "SLEEPER_ID_MAP", id_map_path)
    monkeypatch.setattr(availability.config, "NFLVERSE_XWALK", xwalk_path)

    def _set(id_map_df=None, xwalk_df=None, slim_df=None, injuries_df=None):
        if id_map_df is not None:
            id_map_df.to_csv(id_map_path, index=False)
        if xwalk_df is not None:
            xwalk_df.to_csv(xwalk_path, index=False)
        monkeypatch.setattr(
            availability.sleeper_snapshots, "list_slim_dates", lambda: [dt.date(2026, 9, 15)]
        )
        monkeypatch.setattr(
            availability.sleeper_snapshots, "read_slim", lambda day: slim_df if slim_df is not None else pd.DataFrame()
        )
        monkeypatch.setattr(
            availability.nflverse_store, "load",
            lambda name, season=None: injuries_df if injuries_df is not None else pd.DataFrame(),
        )

    return _set


def test_three_sources_agreeing(wired):
    id_map = pd.DataFrame([{"sleeper_id": "s101", "espn_player_id": 101, "source": "name"}])
    xwalk = pd.DataFrame([{"gsis_id": "00-101", "espn_player_id": 101}])
    slim = pd.DataFrame(
        [{"sleeper_id": "s101", "injury_status": "Questionable", "injury_body_part": "Ankle",
          "injury_notes": "twisted", "practice_participation": "LP"}]
    )
    injuries = pd.DataFrame(
        [{"season": 2026, "week": 2, "gsis_id": "00-101", "report_status": "Questionable",
          "practice_status": "Limited", "report_primary_injury": "Ankle"}]
    )
    wired(id_map_df=id_map, xwalk_df=xwalk, slim_df=slim, injuries_df=injuries)

    result = availability.read(_players_df().iloc[[0]], season=2026, week=2)
    row = result.iloc[0]
    assert row["espn_injury_status"] == "Questionable"
    assert row["sleeper_tier"] == "COIN_FLIP"
    assert row["nflverse_report_status"] == "Questionable"
    assert row["tier"] == "Questionable"  # nflverse wins precedence, and all three agree anyway


def test_three_sources_disagreeing_uses_stated_precedence(wired):
    """nflverse report_status wins over Sleeper's tier and ESPN's status."""
    id_map = pd.DataFrame([{"sleeper_id": "s102", "espn_player_id": 102, "source": "name"}])
    xwalk = pd.DataFrame([{"gsis_id": "00-102", "espn_player_id": 102}])
    slim = pd.DataFrame(
        [{"sleeper_id": "s102", "injury_status": "Questionable", "injury_body_part": None,
          "injury_notes": None, "practice_participation": "DNP"}]
    )
    injuries = pd.DataFrame(
        [{"season": 2026, "week": 2, "gsis_id": "00-102", "report_status": "Doubtful",
          "practice_status": "DNP", "report_primary_injury": "Hamstring"}]
    )
    wired(id_map_df=id_map, xwalk_df=xwalk, slim_df=slim, injuries_df=injuries)

    result = availability.read(_players_df().iloc[[1]], season=2026, week=2)
    row = result.iloc[0]
    assert row["espn_injury_status"] == "ACTIVE"
    assert row["sleeper_tier"] == "HIGH_RISK"
    assert row["nflverse_report_status"] == "Doubtful"
    assert row["tier"] == "Doubtful"  # nflverse beats Sleeper's HIGH_RISK and ESPN's ACTIVE


def test_no_designation_falls_through_when_week_has_data(wired):
    """The (season, week) slice has rows for other players, so this
    player's null report_status means "no designation" -- it must fall
    through to Sleeper/ESPN rather than reading as insufficient data."""
    id_map = pd.DataFrame([{"sleeper_id": "s103", "espn_player_id": 103, "source": "name"}])
    xwalk = pd.DataFrame([{"gsis_id": "00-103", "espn_player_id": 103}])
    slim = pd.DataFrame(
        [{"sleeper_id": "s103", "injury_status": None, "injury_body_part": None,
          "injury_notes": None, "practice_participation": "FP"}]
    )
    injuries = pd.DataFrame(
        [{"season": 2026, "week": 2, "gsis_id": "00-999", "report_status": "Out",
          "practice_status": "DNP", "report_primary_injury": "Knee"}]
    )
    wired(id_map_df=id_map, xwalk_df=xwalk, slim_df=slim, injuries_df=injuries)

    result = availability.read(_players_df().iloc[[2]], season=2026, week=2)
    row = result.iloc[0]
    assert row["nflverse_report_status"] is None
    assert row["tier"] == "CLEAR"  # Sleeper reports CLEAR, ESPN ACTIVE -- not insufficient data


def test_nflverse_feed_entirely_dark_renders_insufficient_data(wired):
    """injuries.parquet not on disk at all -> store.load returns an empty
    frame -> every player's tier must render insufficient data, not a
    guess based on the other two sources."""
    wired(injuries_df=pd.DataFrame(columns=["season", "week", "gsis_id", "report_status"]))

    result = availability.read(_players_df(), season=2026, week=2)
    assert (result["tier"] == availability.INSUFFICIENT_DATA).all()


def test_week_not_yet_published_renders_insufficient_data(wired):
    """The injuries feed has data, but none for this (season, week) --
    indistinguishable from "dark" for this player-slice's purposes."""
    id_map = pd.DataFrame([{"sleeper_id": "s101", "espn_player_id": 101, "source": "name"}])
    xwalk = pd.DataFrame([{"gsis_id": "00-101", "espn_player_id": 101}])
    injuries = pd.DataFrame(
        [{"season": 2026, "week": 1, "gsis_id": "00-101", "report_status": "Questionable",
          "practice_status": "Limited", "report_primary_injury": "Ankle"}]
    )
    wired(id_map_df=id_map, xwalk_df=xwalk, injuries_df=injuries)

    result = availability.read(_players_df().iloc[[0]], season=2026, week=2)
    assert result.iloc[0]["tier"] == availability.INSUFFICIENT_DATA
