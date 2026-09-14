"""Static configuration and credential loading."""

import os
from pathlib import Path

LEAGUE_ID = 1681721675
SEASON = 2026
TEAM_ID = 5

SPORT = "ffl"
BASE = f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/{SPORT}"

# leaguedefaults/3 exposes the player pool without league membership.
PLAYER_POOL_ID = 3

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
OUT_DIR = PROJECT_ROOT / "data" / "out"

# Sleeper player-status layer -- entirely separate store, ISO dates (see
# espn_ff/sleeper/snapshots.py for why).
SLEEPER_DIR = PROJECT_ROOT / "data" / "sleeper"
SLEEPER_RAW_DIR = PROJECT_ROOT / "data" / "raw" / "sleeper"
SLEEPER_SLIM_DIR = SLEEPER_DIR / "slim"
SLEEPER_ID_MAP = SLEEPER_DIR / "player_id_map.csv"

USER_AGENT = "Mozilla/5.0"

# Regular season is 18 weeks; ESPN scoring periods run 1..18.
MAX_SCORING_PERIOD = 18


def load_dotenv(path=None):
    """Populate os.environ from a .env file. Existing vars win."""
    path = Path(path) if path else PROJECT_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def cookies():
    """Session cookies for private leagues, or {} if unset."""
    load_dotenv()
    espn_s2, swid = os.environ.get("ESPN_S2"), os.environ.get("SWID")
    if espn_s2 and swid:
        return {"espn_s2": espn_s2, "SWID": swid}
    return {}


def has_credentials():
    return bool(cookies())
