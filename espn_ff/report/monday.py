"""Monday -- the Monday-night call.

The only report issued while a game is still swappable: a player in
tonight's game unlocks at 10:30 ET, so this is the last chance to bench a
hurt starter, start a healthy alternative, or play for ceiling versus floor
off the live margin. See docs/report-weekly-schedule.md's "Monday --
Monday night call" section for the full spec this module implements.

Assembled in the mandated three-part order: freshness header, body
decisions-first, "what this report cannot see" footer.
"""

import pandas as pd

from .. import config
from . import availability, pool, schedule
from .loaders import freshness, latest_export

INSUFFICIENT_DATA = "insufficient data"

# eligible_slots on player-pool.csv lists FLEX/OP -- slots this league does
# not roster. Only slots this league actually has (roster-slots.csv) are
# ever legal targets.
_IGNORED_LEAGUE_SLOTS = {"FLEX", "OP"}


def league_slots(roster_slots_df):
    """The set of lineup slot names this league actually rosters."""
    if roster_slots_df is None or roster_slots_df.empty:
        return set()
    return set(roster_slots_df["slot"]) - _IGNORED_LEAGUE_SLOTS


def _team_row(matchups_df, week, team_id):
    subset = matchups_df[(matchups_df["week"] == week) & (matchups_df["team_id"] == team_id)]
    return subset.iloc[0] if not subset.empty else None


def live_margin(matchups_df, week, team_id):
    """Our and the opponent's live points for `week`, and the margin
    (opponent - ours; positive means we're behind). `insufficient` is True
    when the matchup can't be read at all, or when both sides read 0.0 --
    the signature of a 09:08 refresh that has not landed yet, not an
    actual 0-0 game."""
    ours = _team_row(matchups_df, week, team_id)
    if ours is None:
        return {"insufficient": True, "reason": "no matchup row for this team/week", "opponent_id": None}

    opponent_id = ours.get("opponent_id")
    opponent_name = ours.get("opponent_name")
    theirs = _team_row(matchups_df, week, opponent_id) if pd.notna(opponent_id) else None

    our_live = ours.get("points_live")
    their_live = theirs.get("points_live") if theirs is not None else None

    # opponent_id/opponent_name come from the matchup structure itself, which
    # is known independent of whether points_live has landed yet -- callers
    # that need "who is our opponent" (e.g. players_in_game) must get it even
    # when the live score cannot be trusted.
    if pd.isna(our_live) or pd.isna(their_live):
        return {
            "insufficient": True, "reason": "points_live missing for one or both sides",
            "opponent_id": opponent_id, "opponent_name": opponent_name,
        }
    if our_live == 0.0 and their_live == 0.0:
        return {
            "insufficient": True,
            "reason": "both sides read 0.0 -- the ESPN refresh likely has not landed",
            "opponent_id": opponent_id, "opponent_name": opponent_name,
        }

    return {
        "insufficient": False,
        "our_points": our_live,
        "their_points": their_live,
        "margin": their_live - our_live,
        "opponent_id": opponent_id,
        "opponent_name": opponent_name,
    }


def players_in_game(rosters_df, week, team_id, opponent_id, monday_teams):
    """Our and the opponent's starters whose pro_team is playing tonight,
    tagged with `side`. Empty frame when neither roster has a player in
    the game -- the common case, and this function does not special-case
    it; callers decide what "no one is at risk" means for their section."""
    if rosters_df.empty or not monday_teams:
        return pd.DataFrame(columns=list(rosters_df.columns) + ["side"])

    week_rosters = rosters_df[rosters_df["week"] == week]
    ours = week_rosters[
        (week_rosters["team_id"] == team_id)
        & (week_rosters["started"])
        & (week_rosters["pro_team"].isin(monday_teams))
    ].copy()
    ours["side"] = "ours"

    theirs = pd.DataFrame(columns=ours.columns)
    if pd.notna(opponent_id):
        theirs = week_rosters[
            (week_rosters["team_id"] == opponent_id)
            & (week_rosters["started"])
            & (week_rosters["pro_team"].isin(monday_teams))
        ].copy()
        theirs["side"] = "opponent"

    return pd.concat([ours, theirs], ignore_index=True)


def eligible_slots_for(player_id, pool_df, allowed_slots):
    """`player_id`'s eligible_slots from player-pool.csv, intersected with
    `allowed_slots` -- weekly-rosters.csv does not carry eligible_slots at
    all, so this must join out to the pool export."""
    if pool_df.empty:
        return set()
    row = pool_df[pool_df["player_id"] == player_id]
    if row.empty:
        return set()
    raw = row["eligible_slots"].iloc[0]
    slots = {s.strip() for s in str(raw or "").split(",") if s.strip()}
    return slots & allowed_slots


def find_alternatives(starter, bench_df, free_agents_df, pool_df, allowed_slots, monday_teams):
    """Bench and free-agent candidates legally eligible for `starter`'s
    slot, restricted to players on one of tonight's two teams, ranked by
    projection. Returns {"bench": df, "free_agents": df, "no_swap": bool}
    -- `no_swap` is True exactly when neither list has a candidate, which
    callers must render as an explicit message, not an empty table."""
    target_slot = starter["lineup_slot"]

    bench_candidates = pd.DataFrame(columns=list(bench_df.columns) if not bench_df.empty else [])
    if not bench_df.empty:
        on_teams = bench_df[
            (bench_df["lineup_slot"] != "IR") & (bench_df["pro_team"].isin(monday_teams))
        ]
        if not on_teams.empty:
            # An empty mask Series can carry a non-bool dtype (e.g. int64),
            # which pandas then misreads as column selection rather than
            # boolean filtering -- guard on `on_teams` being non-empty first.
            mask = on_teams["player_id"].apply(
                lambda pid: target_slot in eligible_slots_for(pid, pool_df, allowed_slots)
            )
            on_teams = on_teams[mask]
        bench_candidates = on_teams.sort_values("projected", ascending=False).reset_index(drop=True)

    fa_candidates = pd.DataFrame(columns=list(free_agents_df.columns) if not free_agents_df.empty else [])
    if not free_agents_df.empty:
        on_teams = free_agents_df[free_agents_df["pro_team"].isin(monday_teams)]
        if not on_teams.empty:
            mask = on_teams["player_id"].apply(
                lambda pid: target_slot in eligible_slots_for(pid, pool_df, allowed_slots)
            )
            on_teams = on_teams[mask]
        fa_candidates = on_teams.sort_values("week_projected", ascending=False).reset_index(drop=True)

    drop_candidate = None
    if not fa_candidates.empty and not bench_df.empty:
        rosterable = bench_df[bench_df["lineup_slot"] != "IR"].sort_values("projected")
        if not rosterable.empty:
            drop_candidate = rosterable.iloc[0]["player_name"]

    return {
        "bench": bench_candidates,
        "free_agents": fa_candidates,
        "drop_candidate": drop_candidate,
        "no_swap": bench_candidates.empty and fa_candidates.empty,
    }


def _fmt_ts(ts):
    if ts is None:
        return "never"
    return pd.Timestamp(ts, unit="s").strftime("%Y-%m-%d %H:%M:%S")


def _freshness_lines(fresh):
    lines = []
    for name in ("sleeper", "nflverse", "espn"):
        ts, stale = fresh[name]
        flag = " (STALE)" if stale else ""
        lines.append(f"- {name}: {_fmt_ts(ts)}{flag}")
    return lines


def render(season, week, team_id, monday_games, margin, at_risk, avail_df, alternatives_by_player, footer_notes):
    lines = []
    lines.append(f"# Monday night call -- {season} week {week}")
    lines.append("")
    lines.append("## Freshness")
    lines.extend(_freshness_lines(freshness(season=season)))
    lines.append("")

    lines.append("## Tonight's game")
    if monday_games.empty:
        lines.append(f"No Monday-night game in week {week}. This report has nothing to add tonight.")
        lines.append("")
        lines.extend(["## What this report cannot see"] + [f"- {n}" for n in footer_notes])
        return "\n".join(lines) + "\n"

    for _, game in monday_games.iterrows():
        lines.append(f"- {game['away_team']} @ {game['home_team']}, {game.get('gametime', '')} ET")
    lines.append("")

    lines.append("## Live margin")
    if margin["insufficient"]:
        lines.append(f"**insufficient data** -- {margin['reason']}")
    else:
        verb = "down" if margin["margin"] > 0 else "up"
        lines.append(
            f"Us {margin['our_points']:.1f} -- {margin.get('opponent_name', 'opponent')} "
            f"{margin['their_points']:.1f} ({verb} {abs(margin['margin']):.1f})"
        )
    lines.append("")

    lines.append("## Who is left")
    if at_risk.empty:
        lines.append("No starter on either side is in tonight's game.")
    else:
        for _, row in at_risk.iterrows():
            lines.append(f"- ({row['side']}) {row['player_name']} -- {row['position']} {row['pro_team']}")
    lines.append("")

    lines.append("## Availability")
    if avail_df.empty:
        lines.append("Nothing at risk tonight.")
    else:
        for _, row in avail_df.iterrows():
            lines.append(
                f"- {row['player_name']}: tier={row['tier']} "
                f"(ESPN={row['espn_injury_status']}, Sleeper={row['sleeper_tier']}, "
                f"nflverse={row['nflverse_report_status']})"
            )
    lines.append("")

    lines.append("## Alternatives")
    ours_at_risk = at_risk[at_risk["side"] == "ours"] if not at_risk.empty else at_risk
    if ours_at_risk.empty:
        lines.append("No at-risk starter on our side tonight.")
    else:
        for _, starter in ours_at_risk.iterrows():
            alt = alternatives_by_player.get(starter["player_id"])
            lines.append(f"### {starter['player_name']} ({starter['lineup_slot']})")
            if alt is None or alt["no_swap"]:
                lines.append("No legal swap exists -- no bench or free-agent candidate is on either "
                              "of tonight's two teams.")
            else:
                for _, cand in alt["bench"].iterrows():
                    lines.append(f"- bench: {cand['player_name']} ({cand['pro_team']}, {cand['projected']:.1f} proj)")
                for _, cand in alt["free_agents"].iterrows():
                    drop = f", drop {alt['drop_candidate']}" if alt["drop_candidate"] else ""
                    lines.append(
                        f"- free agent: {cand['player_name']} ({cand['pro_team']}, "
                        f"{cand['week_projected']:.1f} proj){drop}"
                    )
    lines.append("")

    lines.append("## What this report cannot see")
    lines.extend(f"- {n}" for n in footer_notes)
    return "\n".join(lines) + "\n"


FOOTER_NOTES = [
    "Official inactives drop roughly 90 minutes before kickoff, in no feed this pipeline touches.",
    "Whether an unowned player can be added on a Monday is a league waiver setting recorded "
    "in no artifact here -- the free-agent half of tonight's alternatives may be unactionable.",
    "Which slots ESPN leaves unlocked on a Monday (bye-week and already-played players "
    "specifically) is platform behavior observed nowhere in this repo.",
]


def build(season, week, team_id=None):
    """Assemble the full Monday report as markdown text."""
    team_id = team_id if team_id is not None else config.TEAM_ID

    monday_games = schedule.remaining_games(season, week, weekday="Monday")
    monday_teams = schedule.teams_in(monday_games)

    matchups_df = latest_export("matchups")
    margin = (
        live_margin(matchups_df, week, team_id)
        if not matchups_df.empty
        else {"insufficient": True, "reason": "no matchups export on disk"}
    )
    opponent_id = margin.get("opponent_id")

    rosters_df = latest_export("weekly-rosters")
    at_risk = players_in_game(rosters_df, week, team_id, opponent_id, monday_teams)

    avail_df = pd.DataFrame()
    if not at_risk.empty:
        avail_df = availability.read(at_risk, season, week)

    pool_df = latest_export("player-pool")
    roster_slots_df = latest_export("roster-slots")
    allowed_slots = league_slots(roster_slots_df)
    free_agents_df = pool.free_agents(week, pool_df=pool_df, rosters_df=rosters_df)

    alternatives_by_player = {}
    if not at_risk.empty:
        our_bench = (
            rosters_df[(rosters_df["week"] == week) & (rosters_df["team_id"] == team_id) & (~rosters_df["started"])]
            if not rosters_df.empty
            else pd.DataFrame()
        )
        for _, starter in at_risk[at_risk["side"] == "ours"].iterrows():
            alternatives_by_player[starter["player_id"]] = find_alternatives(
                starter, our_bench, free_agents_df, pool_df, allowed_slots, monday_teams
            )

    return render(
        season, week, team_id, monday_games, margin, at_risk, avail_df, alternatives_by_player, FOOTER_NOTES
    )
