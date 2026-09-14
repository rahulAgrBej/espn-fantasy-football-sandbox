"""nflverse role-feature layer.

ESPN publishes availability (roster, injury status) but nothing about role --
how much of the offence a player actually accounts for. nflverse's public
release assets carry `offense_pct` per player-game and `target_share` /
`air_yards_share` / `wopr` per player-week, rebuilt several times a day.
Joining those onto the ESPN roster is what this package is for.

Boundaries mirror espn_ff/sleeper/: `client.py` talks HTTP only, `store.py`
owns disk and freshness, `datasets.py` is the schema contract, `ids.py` is
the gsis_id <-> espn_id crosswalk, and `features.py` is the pure offline
DuckDB build of the derived table. `gsis_id` is nflverse's own stable id and
is the internal primary key everywhere in this package; `espn_id` is
translated only at the ESPN boundary (espn_ff/nflverse/ids.py).
"""
