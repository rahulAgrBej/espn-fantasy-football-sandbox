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

# nflverse role-feature layer -- raw release-asset parquet mirrored to disk
# and queried in place by DuckDB; no persistent .duckdb file.
NFLVERSE_DIR = PROJECT_ROOT / "data" / "nflverse"
NFLVERSE_RAW_DIR = PROJECT_ROOT / "data" / "raw" / "nflverse"
NFLVERSE_MANIFEST = NFLVERSE_DIR / "manifest.json"
NFLVERSE_XWALK = NFLVERSE_DIR / "player_xwalk.csv"
NFLVERSE_FEATURES = NFLVERSE_DIR / "player_week_features.parquet"

# The Odds API betting-market layer -- metered, unlike the three feeds above.
# ledger.db is the credit ledger; the two parquet files are append-only
# snapshot archives that are never pruned (see espn_ff/odds/store.py).
ODDS_DIR = PROJECT_ROOT / "data" / "odds"
ODDS_RAW_DIR = PROJECT_ROOT / "data" / "raw" / "odds"
ODDS_LEDGER = ODDS_DIR / "ledger.db"
ODDS_PROPS = ODDS_DIR / "player_props.parquet"
ODDS_TEAM_TOTALS = ODDS_DIR / "team_totals.parquet"
ODDS_SCORING = ODDS_DIR / "league_scoring.json"

ODDS_QUOTA = 500
ODDS_RESERVE = 40
ODDS_WARN_THRESHOLD = 100
# Applied instead of ODDS_QUOTA until ODDS_QUOTA_RESET_DAY is observed and
# configured -- see espn_ff/odds/ledger.py:effective_quota.
ODDS_SAFETY_CAP = 400

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


def odds_api_key():
    """The Odds API key, or a fix-it error -- unlike ESPN's cookies, this
    credential is metered, so a caller must be able to tell "unset" apart
    from "empty" rather than silently getting an unauthenticated 401."""
    load_dotenv()
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        # Imported locally: espn_ff.odds.ledger imports this module, so a
        # module-level import here would be circular.
        from .odds.ledger import OddsError

        raise OddsError(
            "ODDS_API_KEY not set -- add it to .env (see .env.example) or export it in your shell."
        )
    return key


def odds_quota_reset_day():
    """Day-of-month the metered quota resets, once observed -- see
    espn_ff/odds/ledger.py:effective_quota. None until ODDS_QUOTA_RESET_DAY
    is set, in which case ODDS_SAFETY_CAP governs instead of ODDS_QUOTA."""
    load_dotenv()
    value = os.environ.get("ODDS_QUOTA_RESET_DAY")
    return int(value) if value else None
