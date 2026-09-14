"""The schema and cost contract -- the espn_ff/nflverse/datasets.py analogue
for this package. Frozen dataclasses, no I/O, no network.

Position -> market-set table is handoff-api §8; `MARKET_STATS` is the
conversion table `projections.py` reads to turn a market's `point`/price
into fantasy points off the league's own scoring, not a hardcoded guess.
"""

from dataclasses import dataclass

from .ledger import OddsError

SPORT = "americanfootball_nfl"
REGIONS = "us"
# <=10; one region's worth of books, and a much smaller payload than the
# full regions=us bookmaker list.
BOOKMAKERS = ("draftkings", "fanduel", "betmgm")


class OddsSchemaError(OddsError):
    """A response is missing a column/key this codebase depends on."""


@dataclass(frozen=True)
class Endpoint:
    path: str  # format template, relative to client.BASE
    free: bool
    cost_formula: str  # human-readable; actual cost computed by the caller


ENDPOINTS = {
    "sports": Endpoint(path="/sports", free=True, cost_formula="0"),
    "events": Endpoint(path="/sports/{sport}/events", free=True, cost_formula="0"),
    "featured_odds": Endpoint(
        path="/sports/{sport}/odds", free=False, cost_formula="markets_requested x regions"
    ),
    "event_odds": Endpoint(
        path="/sports/{sport}/events/{event_id}/odds", free=False,
        cost_formula="unique_markets_returned x regions (estimate: markets_requested)",
    ),
    "scores": Endpoint(
        path="/sports/{sport}/scores", free=False, cost_formula="1, or 2 with daysFrom"
    ),
}

# handoff §8: position present in a game -> the prop markets that game needs.
# D/ST gets no prop market -- implied team totals (from featured odds) are
# its only signal here.
POSITION_MARKETS = {
    "QB": frozenset({"player_pass_yds", "player_pass_tds"}),
    "RB": frozenset({"player_rush_yds", "player_receptions", "player_anytime_td"}),
    "WR": frozenset({"player_reception_yds", "player_receptions", "player_anytime_td"}),
    "TE": frozenset({"player_reception_yds", "player_receptions", "player_anytime_td"}),
    "K": frozenset({"player_kicking_points"}),
    "D/ST": frozenset(),
}

# market key -> (ESPN stat abbreviation it converts into, conversion kind).
# "line" markets convert a book's median point value 1:1 into that stat;
# "prob" markets convert a de-vigged implied probability into an expected
# stat count; "points" markets are already stated in points, not a raw stat.
MARKET_STATS = {
    "player_pass_yds": ("YDS_PASS", "line"),
    "player_pass_tds": ("TD_PASS", "line"),
    "player_rush_yds": ("YDS_RUSH", "line"),
    "player_receptions": ("REC", "line"),
    "player_reception_yds": ("YDS_REC", "line"),
    "player_anytime_td": ("TD", "prob"),
    "player_kicking_points": ("PTS_KICK", "points"),
}


def markets_for_event(roster_rows):
    """roster_rows: iterable of position strings (or dicts with a
    "position" key) present in one event. Returns the deduplicated union of
    every rostered position's market set -- player_anytime_td and
    player_receptions are shared across positions and must appear once."""
    markets = set()
    for row in roster_rows:
        position = row["position"] if isinstance(row, dict) else row
        markets |= POSITION_MARKETS.get(position, frozenset())
    return markets


def assert_schema(payload, kind):
    """Subset check on a response's shape, same contract as
    espn_ff.nflverse.datasets.assert_schema -- fails loudly the moment a
    field this codebase depends on disappears, but tolerates upstream
    additions. `kind` is one of "event", "featured_odds_row",
    "event_odds_row", "score_row"."""
    required = _REQUIRED[kind]
    if isinstance(payload, list):
        for item in payload:
            missing = required - set(item)
            if missing:
                raise OddsSchemaError(f"{kind}: missing expected key(s) {sorted(missing)}")
        return
    missing = required - set(payload)
    if missing:
        raise OddsSchemaError(f"{kind}: missing expected key(s) {sorted(missing)}")


_REQUIRED = {
    "event": frozenset({"id", "commence_time", "home_team", "away_team"}),
    "featured_odds_row": frozenset({"id", "commence_time", "home_team", "away_team", "bookmakers"}),
    "event_odds_row": frozenset({"id", "bookmakers"}),
    "score_row": frozenset({"id", "completed", "home_team", "away_team"}),
}
