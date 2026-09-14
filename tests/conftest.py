import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIXTURES = Path(__file__).parent / "fixtures"
NFLVERSE_FIXTURES = FIXTURES / "nflverse"


@pytest.fixture
def nflverse_players_df():
    return pd.read_csv(
        NFLVERSE_FIXTURES / "players.csv",
        dtype={"gsis_id": str, "pfr_id": str, "espn_id": str},
    )


@pytest.fixture
def nflverse_snaps_df():
    return pd.read_csv(NFLVERSE_FIXTURES / "snap_counts.csv", dtype={"pfr_player_id": str})


@pytest.fixture
def nflverse_stats_df():
    return pd.read_csv(NFLVERSE_FIXTURES / "stats_player.csv", dtype={"player_id": str})


@pytest.fixture
def nflverse_games_df():
    return pd.read_csv(NFLVERSE_FIXTURES / "games.csv")


@pytest.fixture
def nflverse_db_playerids_df():
    return pd.read_csv(NFLVERSE_FIXTURES / "db_playerids.csv", dtype={"gsis_id": str, "espn_id": str})


@pytest.fixture(scope="session")
def player_pool():
    """Five real players from the public 2026 pool.

    Chosen deliberately: some list their 2026 week-1 row first in stats[],
    others list 2025 first. That split is what makes the naive filter's
    failure reproducible.
    """
    return json.loads((FIXTURES / "player_pool_small.json").read_text())


@pytest.fixture(scope="session")
def by_name(player_pool):
    return {p["player"]["fullName"]: p["player"] for p in player_pool["players"]}


@pytest.fixture(scope="session")
def order_unlucky(by_name):
    """Player whose stats[] lists 2025 before 2026 -- naive filter picks wrong."""
    return by_name["Josh Allen"]


@pytest.fixture(scope="session")
def order_lucky(by_name):
    """Player whose stats[] happens to list 2026 first -- naive filter survives."""
    return by_name["Jahmyr Gibbs"]
