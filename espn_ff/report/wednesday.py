"""Wednesday -- the availability watchlist.

The first of the week's three practice days, and deliberately the least
actionable report of the seven: a single Wednesday DNP is one-third of a
trajectory, not a call. See docs/report-weekly-schedule.md's "Wednesday --
availability watchlist" section for the spec this module implements.

Three things this module exists to get right.

First: `tier` off `availability.read` carries three vocabularies at once --
nflverse's `report_status` ("Out"/"Doubtful"/"Questionable"), Sleeper's own
tier (OUT/HIGH_RISK/COIN_FLIP/...), and ESPN's `injury_status` -- because
that function resolves precedence across three sources without collapsing
their labels. Filtering the watchlist on the bare Sleeper tier names would
silently drop every nflverse-sourced row, i.e. the highest-confidence
source. `is_at_risk` normalizes instead.

Second: `practice_trajectory` reads `Wed / -- / --` today *by construction*,
not because a feed is missing. It is rendered anyway, so the reader sees how
much of the signal exists rather than inferring it.

Third: `pos_rank` is not shown at all. It comes from nflverse
`depth_charts`, which docs/data-sources.md flags as not week-aligned -- an
append-only log with no week column -- so reading it as this week's depth is
wrong. Only Sleeper's own `depth_chart_order` delta appears, and
`depth_chart_days_used` is rendered beside it because the delta falls back to
the oldest available snapshot when none sits at exactly --depth-lookback days.

No drop candidates are produced. Dropping on one day of practice data
discards a player before the signal that would justify it exists.
"""

import time

import pandas as pd

from .. import config, weeks
from ..sleeper import signals as sleeper_signals
from ..sleeper import snapshots as sleeper_snapshots
from . import availability, monday, tuesday
from .loaders import freshness, latest_export
from .render import INSUFFICIENT_DATA, freshness_lines, header_lines, num, table
from .waivers import week_projection

DEPTH_LOOKBACK_DAYS = 3

# Sleeper's own tier names. `availability.read` may instead hand back an
# nflverse report_status or an ESPN injury_status, which is why nothing
# filters on this set directly -- see is_at_risk.
_SLEEPER_AT_RISK = {"OUT", "HIGH_RISK", "COIN_FLIP"}

# nflverse report_status / ESPN injury_status values that mean the same
# thing as the three above. QUESTIONABLE maps in because a nflverse-sourced
# "Questionable" is exactly the case Sleeper would have tiered COIN_FLIP or
# HIGH_RISK; leaving it out would make the official feed *less* likely to
# put a player on the watchlist than the unofficial one.
_STATUS_AT_RISK = {"OUT", "DOUBTFUL", "QUESTIONABLE", "IR", "PUP", "SUSPENSION", "SUS"}

_AT_RISK = _SLEEPER_AT_RISK | _STATUS_AT_RISK


def is_at_risk(tier):
    """True when `tier` names a watchlist-worthy availability state, across
    all three vocabularies `availability.read` can return it in. INSUFFICIENT
    DATA is **not** at-risk: it means the nflverse week has no rows at all,
    which is a feed gap reported in the footer, not a designation."""
    if tier is None or (isinstance(tier, float) and pd.isna(tier)):
        return False
    return str(tier).strip().upper().replace(" ", "_") in _AT_RISK


def _today_et(rendered_at):
    """The ET calendar date of this render. Derived from `rendered_at`
    rather than `date.today()` so a runner in UTC cannot roll the
    practice-week arithmetic onto the wrong day."""
    return pd.Timestamp(rendered_at, unit="s", tz="UTC").tz_convert(weeks.ET).date()


def _snapshots_by_date():
    return {day: sleeper_snapshots.read_slim(day) for day in sleeper_snapshots.list_slim_dates()}


def practice_signals(players_df, today, lookback_days=DEPTH_LOOKBACK_DAYS,
                     snapshots_by_date=None, sleeper_df=None):
    """Per-player practice trajectory and depth-chart delta, keyed on ESPN
    `player_id`.

    Reconstructed from our own daily slim snapshots (Sleeper publishes no
    history), so a player with no Sleeper id match, or a week with no
    snapshot on the relevant day, renders the gap rather than a zero.
    `snapshots_by_date`/`sleeper_df` are injectable so this stays testable
    without disk.

    Returns a DataFrame with one row per input player:
    player_id, sleeper_id, practice_participation, practice_trajectory,
    depth_chart_order, depth_chart_improved, depth_chart_promoted,
    depth_chart_days_used, matched.
    """
    snapshots_by_date = _snapshots_by_date() if snapshots_by_date is None else snapshots_by_date
    sleeper_df = availability.sleeper_by_espn_id() if sleeper_df is None else sleeper_df

    rows = []
    for _, player in players_df.iterrows():
        player_id = player["player_id"]
        match = (
            sleeper_df[sleeper_df["espn_player_id"] == player_id]
            if not sleeper_df.empty
            else pd.DataFrame()
        )
        if match.empty:
            rows.append({
                "player_id": player_id, "sleeper_id": None, "practice_participation": None,
                "practice_trajectory": None, "depth_chart_order": None,
                "depth_chart_improved": None, "depth_chart_promoted": None,
                "depth_chart_days_used": None, "matched": False,
            })
            continue

        current = match.iloc[0]
        sleeper_id = current["sleeper_id"]
        trajectory = sleeper_signals.practice_trajectory(snapshots_by_date, sleeper_id, today=today)
        delta = sleeper_signals.depth_chart_delta(
            current, snapshots_by_date, sleeper_id, lookback_days=lookback_days, today=today
        )
        rows.append({
            "player_id": player_id,
            "sleeper_id": sleeper_id,
            "practice_participation": current.get("practice_participation"),
            "practice_trajectory": sleeper_signals.format_trajectory(trajectory),
            "depth_chart_order": current.get("depth_chart_order"),
            "depth_chart_improved": delta["improved"] if delta else None,
            "depth_chart_promoted": delta["promoted"] if delta else None,
            "depth_chart_days_used": delta["days_used"] if delta else None,
            "matched": True,
        })
    return pd.DataFrame(rows)


def our_starters(rosters_df, week, team_id):
    """This week's started rows for `team_id`. Empty frame -- not an
    exception -- when the export is missing, so the caller renders
    insufficient data rather than crashing a scheduled run."""
    if rosters_df.empty:
        return rosters_df
    week_rosters = rosters_df[(rosters_df["week"] == week) & (rosters_df["team_id"] == team_id)]
    return week_rosters[week_rosters["started"]]


def best_replacement(starter, bench, pool_df, allowed_slots, avail_by_id):
    """The highest-projected bench player legally eligible for `starter`'s
    slot, or None when no legal swap exists -- which callers must render as
    an explicit line, never an empty cell.

    The replacement's own `tier` is carried rather than used to filter:
    swapping one COIN_FLIP for another is a judgment the reader makes with
    both tiers visible, not one this function makes by hiding a candidate.

    Returns (row_dict_or_None, fallback_names, nan_names).
    """
    target_slot = starter["lineup_slot"]
    fallback_names, nan_names = set(), set()

    if bench.empty:
        return None, fallback_names, nan_names

    best = None
    for _, bp in bench.iterrows():
        bp_slots, used_fallback = tuesday._eligible_slots(
            bp["player_id"], bp["position"], pool_df, allowed_slots
        )
        if used_fallback:
            fallback_names.add(bp["player_name"])
        if target_slot not in bp_slots:
            continue
        value, source = week_projection(bp["player_id"], pool_df, bp)
        if value is None or pd.isna(value):
            nan_names.add(bp["player_name"])
            continue
        if best is None or value > best["projection"]:
            best = {
                "player_name": bp["player_name"], "position": bp["position"],
                "projection": value, "source": source,
                "tier": avail_by_id.get(bp["player_id"], INSUFFICIENT_DATA),
            }
    return best, fallback_names, nan_names


def watchlist(starters, bench, avail_df, signals_df, pool_df, allowed_slots):
    """At-risk starters ranked by projected points at risk, each paired with
    its best legal bench replacement.

    "Points at risk" is the starter's own week projection, not a delta
    against the replacement: the quantity in play if this player does not
    play is what they were projected to score. The replacement's projection
    is a separate column so the reader can subtract if they want to -- this
    function does not do it for them, because a missing replacement would
    then make the risk itself unreadable.

    Returns (rows, fallback_names, nan_names).
    """
    if starters.empty or avail_df.empty:
        return [], set(), set()

    avail_by_id = dict(zip(avail_df["player_id"], avail_df["tier"]))
    signals_by_id = {r["player_id"]: r for _, r in signals_df.iterrows()} if not signals_df.empty else {}
    avail_rows = {r["player_id"]: r for _, r in avail_df.iterrows()}

    fallback_names, nan_names, rows = set(), set(), []
    for _, starter in starters.iterrows():
        player_id = starter["player_id"]
        tier = avail_by_id.get(player_id)
        if not is_at_risk(tier):
            continue

        projection, proj_source = week_projection(player_id, pool_df, starter)
        if projection is None or pd.isna(projection):
            nan_names.add(starter["player_name"])

        replacement, fb, nan = best_replacement(starter, bench, pool_df, allowed_slots, avail_by_id)
        fallback_names |= fb
        nan_names |= nan

        signal = signals_by_id.get(player_id, {})
        source_row = avail_rows.get(player_id, {})
        rows.append({
            "player_name": starter["player_name"],
            "slot": starter["lineup_slot"],
            "position": starter["position"],
            "pro_team": starter.get("pro_team"),
            "tier": tier,
            "espn_injury_status": source_row.get("espn_injury_status"),
            "sleeper_tier": source_row.get("sleeper_tier"),
            "nflverse_report_status": source_row.get("nflverse_report_status"),
            "practice_trajectory": signal.get("practice_trajectory"),
            "projection": projection,
            "projection_source": proj_source,
            "replacement": replacement,
        })

    # NaN projections sort last rather than being dropped -- an unscored
    # at-risk starter is still on the watchlist, it just cannot be ranked.
    rows.sort(key=lambda r: (r["projection"] is None or pd.isna(r["projection"]),
                             -(r["projection"] if r["projection"] is not None
                               and not pd.isna(r["projection"]) else 0.0)))
    return rows, fallback_names, nan_names


def depth_chart_moves(signals_df, roster_names):
    """Rows where Sleeper's depth_chart_order improved or the player crossed
    into order 1/2 at their own position. `days_used` rides along on every
    row because the delta silently falls back to the oldest available
    snapshot when nothing sits at exactly the lookback distance."""
    if signals_df.empty:
        return []
    rows = []
    for _, r in signals_df.iterrows():
        if not (r.get("depth_chart_improved") or r.get("depth_chart_promoted")):
            continue
        rows.append({
            "player_name": roster_names.get(r["player_id"], r["player_id"]),
            "depth_chart_order": r.get("depth_chart_order"),
            "improved": bool(r.get("depth_chart_improved")),
            "promoted": bool(r.get("depth_chart_promoted")),
            "days_used": r.get("depth_chart_days_used"),
        })
    return rows


FOOTER_NOTES = [
    "One practice day is one-third of a trajectory. `tier` is derived from today's snapshot "
    "alone and will move as Thursday's and Friday's practice reports land.",
    "`practice_trajectory` reads `Wed / -- / --` today by construction, not because a feed "
    "failed. A `-- / -- / --` row means no Sleeper match or no snapshot on that day, and that "
    "gap is permanent -- Sleeper publishes no history to backfill from.",
    "`depth_chart_days_used` is the gap actually used, which falls back to the oldest available "
    "snapshot when none exists at exactly the lookback distance. Read it before trusting either "
    "depth-chart delta.",
    "`pos_rank` is deliberately absent. It comes from nflverse `depth_charts`, an append-only "
    "log with no week column, so reading it as this week's depth is wrong.",
    "`report_status` is null both when the nflverse injuries feed is unavailable and when a "
    "player carries no designation; those two are indistinguishable in that column.",
    "No drop candidates today. Dropping on one day of practice data discards a player before "
    "the signal that would justify it exists.",
    "Whether this league's waiver deadline falls tonight is recorded in no artifact here -- "
    "the deadline note above is an assumption, not a schedule.",
]


def render(season, week, team_id, watch_rows, signals_df, avail_df, starters, moves,
           footer_notes, window=None, rendered_at=None):
    """Pure over its arguments -- no disk access beyond the freshness header,
    same contract as monday.render/waivers.render, so the whole body is
    exercisable from fixtures."""
    rendered_at = rendered_at if rendered_at is not None else time.time()
    today = _today_et(rendered_at)
    title = f"Availability watchlist -- {season} week {week}"
    covers = f"Wed {today} -- the first of week {week}'s three practice days"

    lines = header_lines(title, week, covers, window, rendered_at)
    lines.append("")

    lines.append("## Freshness")
    lines.extend(freshness_lines(freshness(season=season)))
    lines.append("")

    lines.append("## Decisions due")
    lines.append(
        "**None binding today.** This is a contingency list, not an action: one practice day "
        "is one-third of the trajectory Friday's lineup lock will read."
    )
    if starters.empty:
        lines.append(f"**{INSUFFICIENT_DATA}** -- no weekly-rosters export for week {week}.")
    else:
        lines.append(
            f"{len(watch_rows)} of {len(starters)} starters are on the watchlist "
            f"as of this morning's snapshot."
        )
    lines.append(
        "_Waiver-deadline note: whether this league processes claims tonight is recorded in no "
        "artifact here. Treat any Wednesday-night deadline as an unconfirmed assumption._"
    )
    lines.append("")

    lines.append("## Watchlist")
    if starters.empty:
        lines.append(f"**{INSUFFICIENT_DATA}** -- no starters could be read for week {week}.")
    elif not watch_rows:
        lines.append("No starter is OUT, HIGH_RISK or COIN_FLIP on today's read.")
    else:
        headers = ["player", "slot", "tier", "practice (W/T/F)", "proj at risk",
                   "best replacement", "repl. proj", "repl. tier"]
        rows = []
        for r in watch_rows:
            repl = r["replacement"]
            rows.append([
                r["player_name"], r["slot"], r["tier"],
                r["practice_trajectory"] or "-- / -- / --",
                num(r["projection"]),
                repl["player_name"] if repl else "no legal swap",
                num(repl["projection"]) if repl else INSUFFICIENT_DATA,
                repl["tier"] if repl else "--",
            ])
        lines.extend(table(headers, rows))
        lines.append("")
        lines.append(
            "_Source columns for the same players, shown unresolved so a disagreement between "
            "the three feeds is visible rather than hidden behind `tier`:_"
        )
        headers = ["player", "ESPN", "Sleeper", "nflverse"]
        rows = [[r["player_name"], r["espn_injury_status"], r["sleeper_tier"],
                 r["nflverse_report_status"]] for r in watch_rows]
        lines.extend(table(headers, rows))
    lines.append("")

    lines.append("## Practice report -- full roster")
    if avail_df.empty or signals_df.empty:
        lines.append(f"**{INSUFFICIENT_DATA}** -- no roster or no Sleeper snapshot to read.")
    else:
        merged = avail_df.merge(signals_df, on="player_id", how="left")
        headers = ["player", "tier", "practice", "trajectory (W/T/F)", "depth order", "Sleeper match"]
        rows = [
            [r["player_name"], r["tier"], r.get("practice_participation"),
             r.get("practice_trajectory") or "-- / -- / --",
             r.get("depth_chart_order"), "yes" if r.get("matched") else "no"]
            for _, r in merged.iterrows()
        ]
        lines.extend(table(headers, rows))
    lines.append("")

    lines.append("## Depth chart moves")
    if not moves:
        lines.append("No rostered player's Sleeper depth-chart order improved over the lookback window.")
    else:
        headers = ["player", "current order", "improved", "promoted", "days used"]
        rows = [[m["player_name"], m["depth_chart_order"], m["improved"], m["promoted"],
                 m["days_used"]] for m in moves]
        lines.extend(table(headers, rows))
    lines.append("")

    lines.append("## Drop candidates")
    lines.append(
        "_None by design._ Dropping on one day of practice data discards a player before the "
        "signal that would justify it exists. Tuesday's waiver report owns the drop list."
    )
    lines.append("")

    lines.append("## What this report cannot see")
    lines.extend(f"- {n}" for n in footer_notes)
    return "\n".join(lines) + "\n"


def build(season, week, team_id=None):
    """Assemble the full Wednesday report as markdown text. `week` is the
    current scoring period with no offset -- Wednesday looks forward at the
    week in progress, unlike tuesday.build's `review_week = week - 1`."""
    team_id = team_id if team_id is not None else config.TEAM_ID
    rendered_at = time.time()
    today = _today_et(rendered_at)

    rosters_df = latest_export("weekly-rosters")
    pool_df = latest_export("player-pool")
    roster_slots_df = latest_export("roster-slots")
    allowed_slots = monday.league_slots(roster_slots_df)

    starters = our_starters(rosters_df, week, team_id)
    week_rosters = (
        rosters_df[(rosters_df["week"] == week) & (rosters_df["team_id"] == team_id)]
        if not rosters_df.empty
        else rosters_df
    )
    bench = tuesday._bench(week_rosters) if not week_rosters.empty else week_rosters

    avail_df = availability.read(week_rosters, season, week) if not week_rosters.empty else pd.DataFrame()
    signals_df = (
        practice_signals(week_rosters, today=today) if not week_rosters.empty else pd.DataFrame()
    )

    watch_rows, fallback_names, nan_names = watchlist(
        starters, bench, avail_df, signals_df, pool_df, allowed_slots
    )
    roster_names = (
        dict(zip(week_rosters["player_id"], week_rosters["player_name"]))
        if not week_rosters.empty
        else {}
    )
    moves = depth_chart_moves(signals_df, roster_names)

    footer_notes = list(FOOTER_NOTES)
    for name, (_, stale) in freshness(season=season).items():
        if stale:
            footer_notes.append(f"The {name} feed is stale as of this report's generation.")
    unmatched = (
        signals_df[~signals_df["matched"]] if not signals_df.empty and "matched" in signals_df else pd.DataFrame()
    )
    if not unmatched.empty:
        names = sorted(roster_names.get(pid, str(pid)) for pid in unmatched["player_id"])
        footer_notes.append(
            "No Sleeper id match -- practice and depth-chart columns are blank, not clear -- for: "
            + ", ".join(names)
        )
    if fallback_names:
        footer_notes.append(
            "Slot eligibility came from the position fallback (no player-pool row) for: "
            + ", ".join(sorted(fallback_names))
        )
    if nan_names:
        footer_notes.append(
            "Projections read NaN and could not be ranked for: " + ", ".join(sorted(nan_names))
        )
    divergence_df = tuesday.slot_map_divergence(pool_df, allowed_slots)
    if not divergence_df.empty:
        footer_notes.append(
            f"{len(divergence_df)} player(s) in this week's pool have eligible_slots that diverge "
            "from the position-fallback map -- the fallback path may be wrong for them specifically."
        )

    return render(
        season, week, team_id, watch_rows, signals_df, avail_df, starters, moves, footer_notes,
        window=weeks.week_window(season, week), rendered_at=rendered_at,
    )
