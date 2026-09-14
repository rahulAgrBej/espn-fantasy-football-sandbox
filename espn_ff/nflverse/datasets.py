"""The dataset registry -- one declarative table, and the schema contract it
backs.

Each entry names the release tag and filename template, whether the file is
season-scoped, whether the feed is allowed to go dark, and the columns this
codebase actually reads. `assert_schema` is a subset check, not an equality
check: nflverse's real files carry many more columns than we use (games.parquet
is ~45 columns wide; we read 8), and a subset check means an unrelated
upstream addition never breaks ingest. It still fails loudly the moment a
column we depend on disappears or gets renamed.
"""

from dataclasses import dataclass


class NflverseSchemaError(RuntimeError):
    """A loaded parquet is missing a column this codebase depends on."""


@dataclass(frozen=True)
class Dataset:
    tag: str
    filename: str
    required: frozenset
    seasonal: bool = False
    optional: bool = False

    def resolved_filename(self, season=None):
        return self.filename.format(season=season) if self.seasonal else self.filename


DATASETS = {
    "players": Dataset(
        tag="players", filename="players.parquet", seasonal=False,
        required=frozenset({"gsis_id", "espn_id", "pfr_id", "display_name", "position", "status"}),
    ),
    "snap_counts": Dataset(
        tag="snap_counts", filename="snap_counts_{season}.parquet", seasonal=True,
        required=frozenset({
            "game_id", "pfr_player_id", "player", "position", "team", "opponent",
            "season", "week", "game_type", "offense_snaps", "offense_pct",
        }),
    ),
    "stats_player": Dataset(
        tag="stats_player", filename="stats_player_week_{season}.parquet", seasonal=True,
        required=frozenset({
            "player_id", "season", "week", "season_type", "team", "position",
            "targets", "target_share", "air_yards_share", "wopr", "fantasy_points_ppr",
        }),
    ),
    "schedules": Dataset(
        tag="schedules", filename="games.parquet", seasonal=False,
        required=frozenset({
            "game_id", "season", "week", "game_type", "home_team", "away_team",
            "home_score", "away_score",
        }),
    ),
    "injuries": Dataset(
        tag="injuries", filename="injuries_{season}.parquet", seasonal=True, optional=True,
        required=frozenset({"season", "week", "team", "gsis_id", "report_status", "practice_status"}),
    ),
    # No `week` column and, unlike every other seasonal dataset here, no
    # `season` column either -- depth_charts is an append-only log keyed on
    # `dt` only. The season lives in the filename, same as everywhere else
    # in this registry; store.load("depth_charts", season=...) collapses the
    # file to its latest `dt` per team on read (see store.py).
    "depth_charts": Dataset(
        tag="depth_charts", filename="depth_charts_{season}.parquet", seasonal=True, optional=True,
        required=frozenset({"dt", "team", "gsis_id", "pos_abb", "pos_rank"}),
    ),
}


def assert_schema(name, columns):
    dataset = DATASETS[name]
    missing = dataset.required - set(columns)
    if missing:
        raise NflverseSchemaError(f"{name}: missing expected column(s) {sorted(missing)}")
