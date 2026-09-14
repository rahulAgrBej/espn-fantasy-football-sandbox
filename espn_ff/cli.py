"""Command line entry point: python -m espn_ff <command>"""

import argparse
import sys
from datetime import date

import pandas as pd

from . import config, constants, stats
from .client import EspnClient, EspnError
from .extract import draft, matchups, players, rosters, settings, teams
from .extract._common import team_name
from .cache import ttl_for
from .extract import transactions as txn
from .sleeper import ids as sleeper_ids
from .sleeper import signals as sleeper_signals
from .sleeper import snapshots as sleeper_snapshots
from .sleeper.client import SleeperClient


def _out_path(name):
    config.OUT_DIR.mkdir(parents=True, exist_ok=True)
    return config.OUT_DIR / f"{date.today():%d-%m-%Y}-{name}.csv"


def _write(df, name):
    if df is None or df.empty:
        print(f"  {name:<22} (empty, not written)")
        return None
    path = _out_path(name)
    df.to_csv(path, index=False)
    print(f"  {name:<22} {len(df):>5} rows -> {path.name}")
    return path


def _weeks(spec, current):
    if not spec:
        return list(range(1, (current or 1) + 1))
    if "-" in spec:
        lo, hi = spec.split("-", 1)
        return list(range(int(lo), int(hi) + 1))
    return [int(w) for w in spec.split(",")]


def _player_names(client, season, weeks):
    """playerId -> name, harvested from the rosters we already fetched."""
    names = {}
    for week in weeks:
        payload = client.get_league(["mRoster"], scoring_period=week)
        for team in payload.get("teams") or []:
            for entry in ((team.get("roster") or {}).get("entries")) or []:
                player = ((entry.get("playerPoolEntry") or {}).get("player")) or {}
                if player.get("id"):
                    names[player["id"]] = player.get("fullName")
    return names


# ---- commands ------------------------------------------------------------


def cmd_probe(client, args):
    payload = client.get_league(["mSettings", "mTeam"])
    meta = settings.league_meta(payload)
    print("Auth OK.\n")
    print(f"League:   {meta['league_name']}  (id {meta['league_id']}, {meta['season']})")
    print(f"Size:     {meta['size']} teams   public={meta['is_public']}")
    print(f"Week:     {meta['current_scoring_period']}")
    print(f"\nTop-level keys returned: {sorted(payload.keys())}")
    print(f"\n{len(payload.get('teams') or [])} teams:")
    for team in payload.get("teams") or []:
        print(f"  {team.get('id'):>3}  {team_name(team)}")
    return 0


def cmd_team(client, args):
    week = args.week or client.current_scoring_period()
    payload = client.get_league(
        ["mSettings", "mTeam", "mRoster"],
        scoring_period=week,
        ttl=ttl_for(week, client.current_scoring_period()),
    )
    meta = settings.league_meta(payload)
    df = rosters.rosters_frame(payload, season=client.season, week=week)
    df = df[df["team_id"] == args.team_id]
    if df.empty:
        raise EspnError(f"No team with id {args.team_id}")

    print(f"League:  {meta['league_name']}  (id {client.league_id}, {client.season})")
    print(f"Team:    {df['team_name'].iloc[0]}  (id {args.team_id})")

    team = next(t for t in payload["teams"] if t["id"] == args.team_id)
    rec = (team.get("record") or {}).get("overall") or {}
    print(
        f"Record:  {rec.get('wins', 0)}-{rec.get('losses', 0)}-{rec.get('ties', 0)}"
        f"   PF {rec.get('pointsFor', 0):.1f}   PA {rec.get('pointsAgainst', 0):.1f}"
    )
    print(f"\nWeek {week} roster")
    print("-" * 60)
    for _, r in df.iterrows():
        print(
            f"{r['lineup_slot']:<6} {str(r['player_name']):<26} {r['position']:<5}"
            f" {r['points']:>6.1f} {r['projected']:>7.1f}"
        )
    print("-" * 60)
    print(f"{'Starters total':<39}{df.loc[df['started'], 'points'].sum():>6.1f}")
    return 0


def cmd_pull(client, args):
    current = client.current_scoring_period()
    weeks = _weeks(args.weeks, current)
    print(f"Pulling league {client.league_id} season {client.season}, weeks {weeks[0]}-{weeks[-1]}")

    client.get_league(["mSettings", "mTeam", "mStandings"])
    client.get_league(["mMatchupScore", "mTeam"])
    client.get_league(["mDraftDetail", "mTeam"])
    client.get_league(["mTransactions2", "mTeam"])
    for week in weeks:
        client.get_league(
            ["mRoster", "mTeam", "mMatchupScore"],
            scoring_period=week,
            ttl=ttl_for(week, current),
        )
        print(f"  week {week:>2} cached")
    client.get_player_pool()
    print("Done. Raw JSON under data/raw/.")
    return 0


def cmd_export(client, args):
    season = client.season
    current = client.current_scoring_period()
    weeks = _weeks(args.weeks, current)
    print(f"Exporting season {season}, weeks {weeks[0]}-{weeks[-1]}\n")

    base = client.get_league(["mSettings", "mTeam", "mStandings"])
    _write(teams.teams_frame(base), "teams")
    _write(settings.scoring_frame(base, season), "scoring-rules")
    _write(settings.roster_slots_frame(base), "roster-slots")

    sched = client.get_league(["mMatchupScore", "mTeam"])
    _write(matchups.matchups_frame(sched, season), "matchups")

    import pandas as pd

    weekly = [
        rosters.rosters_frame(
            client.get_league(
                ["mRoster", "mTeam"], scoring_period=w, ttl=ttl_for(w, current)
            ),
            season=season,
            week=w,
        )
        for w in weeks
    ]
    weekly = [df for df in weekly if not df.empty]
    roster_df = pd.concat(weekly, ignore_index=True) if weekly else pd.DataFrame()
    _write(roster_df, "weekly-rosters")

    names = (
        dict(zip(roster_df["player_id"], roster_df["player_name"]))
        if not roster_df.empty
        else {}
    )
    _write(draft.draft_frame(client.get_league(["mDraftDetail", "mTeam"]), season, names), "draft")
    _write(
        txn.transactions_frame(client.get_league(["mTransactions2", "mTeam"]), season, names),
        "transactions",
    )
    _write(players.players_frame(client.get_player_pool(), season, current), "player-pool")
    return 0


def _read_id_map():
    if config.SLEEPER_ID_MAP.exists():
        return pd.read_csv(config.SLEEPER_ID_MAP)
    return pd.DataFrame(columns=sleeper_ids.MAP_COLUMNS)


def _write_id_map(map_df):
    config.SLEEPER_DIR.mkdir(parents=True, exist_ok=True)
    map_df.to_csv(config.SLEEPER_ID_MAP, index=False)


def cmd_sleeper(client, args):
    """Daily batch job: fetch + slim the Sleeper player pool, trending,
    prune old snapshots, and resolve ids against ESPN. No terminal
    rendering -- run `status` afterward to build the CSV."""
    s_client = SleeperClient()

    df, from_cache = sleeper_snapshots.fetch_players(client=s_client, refresh=args.refresh)
    print(f"Sleeper players: {len(df)} active rows{' (cached, already ran today)' if from_cache else ''}")

    adds = sleeper_snapshots.fetch_trending("add", client=s_client, limit=args.trending_limit)
    drops = sleeper_snapshots.fetch_trending("drop", client=s_client, limit=args.trending_limit)
    sleeper_snapshots.write_trending(adds, drops)
    print(f"Trending: {len(adds)} adds, {len(drops)} drops")

    espn_players = players.players_frame(client.get_player_pool(), season=client.season)
    map_df, unmatched = sleeper_ids.resolve(df, espn_players, existing_map=_read_id_map())
    _write_id_map(map_df)
    if unmatched:
        print(f"  {len(unmatched)} Sleeper player(s) unmatched to ESPN (logged, not dropped): {unmatched[:10]}")

    current = client.current_scoring_period()
    roster_payload = client.get_league(
        ["mRoster", "mTeam"], scoring_period=current, ttl=ttl_for(current, current)
    )
    roster_df = rosters.rosters_frame(roster_payload, season=client.season, week=current)
    team_espn_ids = set(roster_df.loc[roster_df["team_id"] == args.team_id, "player_id"])
    missing = sleeper_ids.missing_from_roster(map_df, team_espn_ids)
    if missing:
        raise EspnError(
            f"{len(missing)} player(s) on team {args.team_id}'s roster have no Sleeper match: "
            f"{missing}. Fix these in {config.SLEEPER_ID_MAP} before trusting the status table."
        )

    print("Done.")
    return 0


def cmd_status(client, args):
    """Build the derived signal table from existing Sleeper snapshots and
    write it to data/out/. No network."""
    today = date.today()
    latest = sleeper_snapshots.read_slim(today)
    if latest is None or latest.empty:
        print("No Sleeper snapshot for today yet -- run `sleeper` first.", file=sys.stderr)
        return 1

    id_map = _read_id_map()
    trending_csv = sleeper_snapshots.trending_path(today)
    trending_df = (
        pd.read_csv(trending_csv)
        if trending_csv.exists()
        else pd.DataFrame(columns=["player_id", "count", "kind"])
    )

    snapshot_dates = sleeper_snapshots.list_slim_dates()
    snapshots_by_date = {d: sleeper_snapshots.read_slim(d) for d in snapshot_dates}

    rows = []
    for _, row in latest.iterrows():
        sleeper_id = row["sleeper_id"]
        tier = sleeper_signals.availability_tier(row.get("injury_status"), row.get("practice_participation"))
        trajectory = sleeper_signals.practice_trajectory(snapshots_by_date, sleeper_id, today=today)
        delta = sleeper_signals.depth_chart_delta(
            row, snapshots_by_date, sleeper_id, lookback_days=args.depth_lookback, today=today
        )
        trending_flag, trending_count = sleeper_signals.trending_flag(
            trending_df, sleeper_id, kind="add", limit=args.trending_limit, floor=args.trending_floor or 0
        )
        espn_match = id_map.loc[id_map["sleeper_id"] == sleeper_id, "espn_player_id"]
        rows.append(
            {
                "sleeper_id": sleeper_id,
                "espn_player_id": espn_match.iloc[0] if not espn_match.empty else None,
                "full_name": row.get("full_name"),
                "team": row.get("team"),
                "position": row.get("position"),
                "injury_status": row.get("injury_status"),
                "practice_participation": row.get("practice_participation"),
                "tier": tier,
                "practice_trajectory": sleeper_signals.format_trajectory(trajectory),
                "depth_chart_order": row.get("depth_chart_order"),
                "depth_chart_improved": delta["improved"] if delta else None,
                "depth_chart_promoted": delta["promoted"] if delta else None,
                "depth_chart_days_used": delta["days_used"] if delta else None,
                "trending_add": trending_flag,
                "trending_add_count": trending_count,
            }
        )
    _write(pd.DataFrame(rows), "sleeper-status")
    return 0


COMMANDS = {
    "probe": cmd_probe,
    "team": cmd_team,
    "pull": cmd_pull,
    "export": cmd_export,
    "sleeper": cmd_sleeper,
    "status": cmd_status,
}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="espn_ff")
    ap.add_argument("command", choices=sorted(COMMANDS))
    ap.add_argument("--season", type=int, default=config.SEASON)
    ap.add_argument("--league-id", type=int, default=config.LEAGUE_ID)
    ap.add_argument("--team-id", type=int, default=config.TEAM_ID)
    ap.add_argument("--week", type=int, help="scoring period; defaults to current")
    ap.add_argument("--weeks", help="range like 1-18 or list like 1,2,5")
    ap.add_argument("--refresh", action="store_true", help="bypass the cache")
    ap.add_argument(
        "--depth-lookback", type=int, default=3,
        help="days back to diff depth_chart_order against (status command)",
    )
    ap.add_argument(
        "--trending-limit", type=int, default=25,
        help="top-N by Sleeper add count to fetch/flag as trending",
    )
    ap.add_argument(
        "--trending-floor", type=int, default=0,
        help="minimum raw add count required to flag as trending",
    )
    args = ap.parse_args(argv)

    client = EspnClient(season=args.season, league_id=args.league_id, refresh=args.refresh)
    try:
        return COMMANDS[args.command](client, args)
    except EspnError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
