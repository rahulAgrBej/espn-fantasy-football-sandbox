"""Command line entry point: python -m espn_ff <command>"""

import argparse
import sys
from datetime import date

import pandas as pd

from . import config, constants, stats
from .client import EspnClient, EspnError, PrivateLeagueError
from .extract import draft, matchups, players, rosters, settings, teams
from .extract._common import team_name
from .cache import ttl_for
from .extract import transactions as txn
from .sleeper import ids as sleeper_ids
from .sleeper import signals as sleeper_signals
from .sleeper import snapshots as sleeper_snapshots
from .sleeper.client import SleeperClient
from .nflverse import features as nflverse_features
from .nflverse import ids as nflverse_ids
from .nflverse import store as nflverse_store
from .nflverse.client import NflverseError
from .odds import jobs as odds_jobs
from .odds import ledger as odds_ledger
from .odds import projections as odds_projections
from .odds.ledger import BudgetExceeded, OddsError
from .report import monday as report_monday
from .report import tuesday as report_tuesday
from .report import waivers as report_waivers

# day key -> (build function, <day> filename segment, output slug). The key
# is usually the day, but Tuesday carries two reports, so "tuesday-waivers"
# shares the "tuesday" filename segment with "tuesday" while remaining its
# own REPORTS entry. cmd_report looks up this table instead of a bare set
# of implemented days so the "not implemented yet" guard and the output
# path both derive from the same source.
REPORTS = {
    "monday": (report_monday.build, "monday", "monday-night-call"),
    "tuesday": (report_tuesday.build, "tuesday", "week-in-review"),
    "tuesday-waivers": (report_waivers.build, "tuesday", "waiver-wire"),
}


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


def _refresh_weeks(spec):
    """--refresh-weeks takes the same spec as --weeks, but with no default:
    omitted means "refresh nothing", not "refresh everything"."""
    return set(_weeks(spec, None)) if spec else set()


def _seasons(spec, current):
    if not spec:
        return [current]
    if "-" in spec:
        lo, hi = spec.split("-", 1)
        return list(range(int(lo), int(hi) + 1))
    return [int(s) for s in spec.split(",")]


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
    print(f"Week:     {client.current_scoring_period()}")
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


def cmd_nflverse(client, args):
    """Daily batch job: refresh the nflverse parquet mirror (players,
    schedules, then the seasonal sets for --seasons), rebuild the player
    crosswalk, and cross-check it against your ESPN roster. No terminal
    rendering of the feature table -- run `features` afterward to build the
    CSV."""
    seasons = _seasons(args.seasons, client.season)
    label = f"{seasons[0]}-{seasons[-1]}" if len(seasons) > 1 else str(seasons[0])
    print(f"nflverse refresh -- season(s) {label}")

    results = nflverse_store.refresh(seasons=seasons, force=args.force)
    for result in results:
        row_label = f"{result.name} {result.season}" if result.season else result.name
        if result.status == "updated":
            detail = f"{result.rows:,} rows  (updated)"
        elif result.status == "unchanged":
            rows = f"{result.rows:,} rows  " if result.rows is not None else ""
            detail = f"{rows}(unchanged)"
        elif result.status == "missing":
            detail = f"(missing -- feed unavailable: {result.message or 'n/a'})"
        else:
            detail = f"(error: {result.message})"
        print(f"  {row_label:<28} {detail}")

    manifest = nflverse_store.read_manifest()
    stale = [key for key, entry in manifest.items() if nflverse_store.is_stale(entry)]
    if stale:
        print(f"  stale (>36h since last successful check): {stale}")

    players_df = nflverse_store.load("players")
    fallback_df = nflverse_ids.fetch_fallback()
    xwalk_df, xwalk_stats = nflverse_ids.build_xwalk(players_df, fallback_df)
    config.NFLVERSE_DIR.mkdir(parents=True, exist_ok=True)
    xwalk_df.to_csv(config.NFLVERSE_XWALK, index=False)
    print(
        f"  crosswalk: {xwalk_stats['total']:,} players, {xwalk_stats['espn_matched']:,} espn-matched "
        f"({xwalk_stats['from_players']:,} from players.parquet, "
        f"{xwalk_stats['from_db_playerids']:,} from db_playerids fallback)"
    )

    current_season = seasons[-1]
    snaps = nflverse_store.load("snap_counts", season=current_season)
    reg_snaps = snaps[(snaps["game_type"] == "REG") & (snaps["position"].isin(nflverse_features.SKILL_POSITIONS))]
    xwalk_by_pfr = xwalk_df.rename(columns={"pfr_id": "pfr_player_id"})
    snap_orphans = nflverse_ids.orphans(reg_snaps, xwalk_by_pfr, on="pfr_player_id", label_cols=["player", "team", "position"])
    if not snap_orphans.empty:
        print(f"  {len(snap_orphans)} snap-count player(s) with no gsis_id match: {snap_orphans['player'].tolist()}")

    current = client.current_scoring_period()
    roster_payload = client.get_league(
        ["mRoster", "mTeam"], scoring_period=current, ttl=ttl_for(current, current)
    )
    roster_df = rosters.rosters_frame(roster_payload, season=client.season, week=current)
    team_roster = roster_df[roster_df["team_id"] == args.team_id]
    matched_espn_ids = set(xwalk_df["espn_player_id"].dropna().astype(int))
    unresolved = team_roster[
        (team_roster["position"] != "D/ST") & (~team_roster["player_id"].isin(matched_espn_ids))
    ]
    if not unresolved.empty:
        # Warns rather than raises -- a just-signed player missing from
        # players.parquet is expected (~1/week) and is exactly the emerging
        # player this layer exists to surface, so it must not block the run.
        print(
            f"  {len(unresolved)} non-D/ST player(s) on team {args.team_id}'s roster have no nflverse "
            f"match (expected for a just-signed player): {unresolved['player_name'].tolist()}"
        )

    print("Done.")
    return 0


def cmd_features(client, args):
    """Build the derived nflverse role-feature table from parquet already on
    disk and write it to data/nflverse/ and data/out/. No network."""
    seasons = _seasons(args.seasons, client.season)
    if not nflverse_store.local_path("players").exists() or not nflverse_store.local_path("schedules").exists():
        print("No nflverse data on disk yet -- run `nflverse` first.", file=sys.stderr)
        return 1

    df = nflverse_features.build(seasons)
    if df.empty:
        print(
            "No feature rows built -- check that `nflverse` has fetched snap_counts/stats_player "
            "for these seasons.",
            file=sys.stderr,
        )
        return 1

    config.NFLVERSE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(config.NFLVERSE_FEATURES, index=False)
    print(f"  player_week_features  {len(df):>5} rows -> {config.NFLVERSE_FEATURES}")

    display_df = df[df["offense_pct"].fillna(0) >= (args.min_snap_pct or 0)]
    _write(display_df, "player-week-features")
    return 0


def _print_credits_footer(con):
    s = odds_ledger.state(con)
    print(f"  credits used / {s['quota']}: {s['spent']}/{s['quota']}  (remaining {s['remaining']})")
    if s["warn"]:
        print(f"  [warning] remaining credits below ODDS_WARN_THRESHOLD ({config.ODDS_WARN_THRESHOLD})")
    if config.odds_quota_reset_day() is None:
        print(f"  [warning] ODDS_QUOTA_RESET_DAY not set -- capped at the {config.ODDS_SAFETY_CAP}-credit safety cap until observed")


def cmd_odds(client, args):
    """Metered batch job dispatcher. Unlike every other command in this
    CLI, each of these jobs can spend real credits -- there is no
    `--refresh`-style "just check again" here. `--dry-run` runs the same
    event-selection walk and prints the estimate without issuing anything
    to the Odds API; `--events` narrows the props/pre_lock pull to an
    explicit set of event ids."""
    if not args.job or args.job not in odds_jobs.JOBS:
        print(f"Usage: odds <job>  where job is one of: {', '.join(odds_jobs.JOBS)}", file=sys.stderr)
        return 1

    con = odds_ledger.open_db()
    event_ids = set(args.events.split(",")) if args.events else None

    if args.dry_run:
        est = odds_jobs.estimate_cost(
            args.job, espn_client=client, team_id=args.team_id, today=None, con=con, event_ids=event_ids
        )
        s = odds_ledger.state(con)
        floor = 0 if args.job == "pre_lock" else config.ODDS_RESERVE
        would_pass = s["spent"] + est <= s["quota"] - floor
        print(f"[dry-run] odds {args.job}: estimated cost {est} credit(s), issuing nothing")
        print(f"  {'would pass' if would_pass else 'WOULD EXCEED BUDGET'} the guard at {s['spent']}/{s['quota']} used")
        _print_credits_footer(con)
        return 0 if would_pass else 1

    if args.job == "results":
        result = odds_jobs.results(con=con)
    elif args.job == "line_movement":
        result = odds_jobs.line_movement(client, con=con)
    elif args.job == "props":
        result = odds_jobs.props_primary(client, team_id=args.team_id, con=con, force=args.force, event_ids=event_ids)
    elif args.job == "pre_lock":
        result = odds_jobs.pre_lock(client, team_id=args.team_id, con=con, event_ids=event_ids)
    else:  # "slate"
        result = odds_jobs.slate_context(client, team_id=args.team_id, con=con)

    print(f"  odds {args.job}: {result}")
    _print_credits_footer(con)
    return 0


def cmd_projections(client, args):
    """Derived betting-market projections, built entirely from disk -- no
    network. Run an `odds` job first; an empty CSV here means nothing has
    been captured yet, not an error."""
    week = args.week or client.current_scoring_period()
    props_points, team_totals_points = odds_projections.build(week=week)
    _write(props_points, "odds-player-props")
    _write(team_totals_points, "odds-team-totals")
    _print_credits_footer(odds_ledger.open_db())
    return 0


def cmd_credits(client, args):
    """Free: reads the ledger only."""
    _print_credits_footer(odds_ledger.open_db())
    return 0


def cmd_report(client, args):
    """Render one of docs/report-weekly-schedule.md's reports from data
    already on disk. No network beyond what `--week`'s default fallback
    needs. Any day not yet in REPORTS exits cleanly rather than writing an
    empty file."""
    if not args.day:
        print(f"Usage: report --day <day>  where day is one of: {', '.join(sorted(REPORTS))}", file=sys.stderr)
        return 1
    if args.day not in REPORTS:
        print(f"report --day {args.day}: not implemented yet -- only {sorted(REPORTS)} is", file=sys.stderr)
        return 1

    build_fn, day_label, slug = REPORTS[args.day]
    season = args.season
    week = args.week or client.current_scoring_period()
    text = build_fn(season, week, team_id=args.team_id)

    out_dir = config.PROJECT_ROOT / "reports" / str(season) / f"week-{week:02d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{date.today():%Y-%m-%d}-{day_label}-{slug}.md"
    path.write_text(text)
    print(f"  wrote {path}")
    return 0


# Exit codes. Unattended runs are alerted on from these alone, so the two
# predictable, actionable failures get their own rather than sharing 1 with
# every transient outage.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BUDGET = 2   # Odds credit guard declined to spend; nothing was issued
EXIT_AUTH = 3     # ESPN session cookies expired or missing

COMMANDS = {
    "probe": cmd_probe,
    "team": cmd_team,
    "pull": cmd_pull,
    "export": cmd_export,
    "sleeper": cmd_sleeper,
    "status": cmd_status,
    "nflverse": cmd_nflverse,
    "features": cmd_features,
    "odds": cmd_odds,
    "projections": cmd_projections,
    "credits": cmd_credits,
    "report": cmd_report,
}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="espn_ff")
    ap.add_argument("command", choices=sorted(COMMANDS))
    ap.add_argument(
        "job", nargs="?",
        help=f"job name for the `odds` command: {', '.join(odds_jobs.JOBS)}",
    )
    ap.add_argument("--season", type=int, default=config.SEASON)
    ap.add_argument("--league-id", type=int, default=config.LEAGUE_ID)
    ap.add_argument("--team-id", type=int, default=config.TEAM_ID)
    ap.add_argument("--week", type=int, help="scoring period; defaults to current")
    ap.add_argument("--weeks", help="range like 1-18 or list like 1,2,5")
    ap.add_argument("--refresh", action="store_true", help="bypass the cache")
    ap.add_argument(
        "--refresh-weeks",
        help="force a re-pull of these scoring periods only, bypassing the cache for "
        "week-scoped ESPN fetches (mRoster, mMatchupScore when fetched with a week): "
        "same spec as --weeks, e.g. 3 or 1-4 or 1,3,5. League-wide views with no "
        "scoringPeriodId (mDraftDetail, mTransactions2, kona_player_info, and "
        "mMatchupScore fetched standalone) still need plain --refresh. "
        "Plain --refresh wins when both are given.",
    )
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
    ap.add_argument(
        "--seasons", help="nflverse seasons: range like 2024-2026 or list like 2024,2026; "
        "defaults to the current season (nflverse/features commands)",
    )
    ap.add_argument(
        "--force", action="store_true",
        help="nflverse: bypass the timestamp short-circuit and re-download every asset. "
        "odds props: bypass the Wednesday weekday check only -- never the credit budget.",
    )
    ap.add_argument(
        "--min-snap-pct", type=float, default=0.0,
        help="display filter on the features CSV: minimum offense_pct to include (default 0)",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="odds command: estimate the credit cost and check the guard without issuing any request",
    )
    ap.add_argument(
        "--events", help="odds props/pre_lock: comma-separated event ids to restrict the pull to",
    )
    ap.add_argument(
        "--day", help=f"report command: which day's report to render, one of {sorted(REPORTS)}",
    )
    args = ap.parse_args(argv)

    client = EspnClient(
        season=args.season,
        league_id=args.league_id,
        refresh=args.refresh,
        refresh_weeks=_refresh_weeks(args.refresh_weeks),
    )
    try:
        return COMMANDS[args.command](client, args)
    except PrivateLeagueError as exc:
        # ESPN's cookies are browser session credentials that expire on their
        # own schedule and cannot be refreshed programmatically. That makes
        # this the one failure here a person must act on, and it should never
        # be confused with ESPN simply being down.
        print(f"\n{exc}", file=sys.stderr)
        return EXIT_AUTH
    except BudgetExceeded as exc:
        # Distinct from a failure on purpose. The credit guard firing means
        # the code worked: it declined to spend and issued nothing. An
        # unattended runner needs to tell that apart from expired cookies or
        # a broken endpoint, both of which need a human.
        print(f"\n{exc}", file=sys.stderr)
        return EXIT_BUDGET
    except (EspnError, NflverseError, OddsError) as exc:
        print(f"\n{exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
