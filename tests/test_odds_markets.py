"""espn_ff.odds.markets -- pure, no I/O."""

import json
from pathlib import Path

import pytest

from espn_ff.odds import markets

FIXTURES = Path(__file__).parent / "fixtures" / "odds"


def test_markets_for_event_unions_and_dedupes_across_positions():
    result = markets.markets_for_event(["RB", "WR", "WR", "TE"])
    assert result == {
        "player_rush_yds", "player_receptions", "player_anytime_td",
        "player_reception_yds",
    }


def test_dst_contributes_no_prop_markets():
    assert markets.markets_for_event(["D/ST"]) == set()


def test_markets_for_event_accepts_dict_rows():
    rows = [{"position": "K"}, {"position": "QB"}]
    assert markets.markets_for_event(rows) == {
        "player_kicking_points", "player_pass_yds", "player_pass_tds",
    }


def test_assert_schema_passes_on_real_shaped_fixtures():
    events = json.loads((FIXTURES / "events.json").read_text())
    featured = json.loads((FIXTURES / "featured_odds.json").read_text())
    event_odds = json.loads((FIXTURES / "event_odds.json").read_text())
    scores = json.loads((FIXTURES / "scores.json").read_text())

    markets.assert_schema(events, "event")
    markets.assert_schema(featured, "featured_odds_row")
    markets.assert_schema(event_odds, "event_odds_row")
    markets.assert_schema(scores, "score_row")


def test_assert_schema_raises_on_missing_column():
    with pytest.raises(markets.OddsSchemaError):
        markets.assert_schema([{"id": "x"}], "event")


def test_bookmakers_default_is_within_the_ten_book_limit():
    assert len(markets.BOOKMAKERS) <= 10
