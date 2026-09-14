import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIXTURES = Path(__file__).parent / "fixtures"


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
