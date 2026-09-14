"""Command line entry point: python -m espn_ff <command>"""

import argparse
import sys
from datetime import date

from . import config, constants, stats
from .client import EspnClient, EspnError
from .extract import draft, matchups, players, rosters, settings, teams
from .extract._common import team_name
from .cache import ttl_for
from .extract import transactions as txn


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


COMMANDS = {"probe": cmd_probe, "team": cmd_team, "pull": cmd_pull, "export": cmd_export}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="espn_ff")
    ap.add_argument("command", choices=sorted(COMMANDS))
    ap.add_argument("--season", type=int, default=config.SEASON)
    ap.add_argument("--league-id", type=int, default=config.LEAGUE_ID)
    ap.add_argument("--team-id", type=int, default=config.TEAM_ID)
    ap.add_argument("--week", type=int, help="scoring period; defaults to current")
    ap.add_argument("--weeks", help="range like 1-18 or list like 1,2,5")
    ap.add_argument("--refresh", action="store_true", help="bypass the cache")
    args = ap.parse_args(argv)

    client = EspnClient(season=args.season, league_id=args.league_id, refresh=args.refresh)
    try:
        return COMMANDS[args.command](client, args)
    except EspnError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
