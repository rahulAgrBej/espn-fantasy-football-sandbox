"""Saturday -- the contingency check.

Friday's lineup lock renders at 11:00 ET, and the official final injury
report for Sunday games is published Friday afternoon -- after Friday's
report has already gone out. Saturday is the only slot that sees it before
kickoff. Everything else here is deliberately small: this is a diff against
Friday, and its correct output on a quiet week is "nothing moved." See
docs/report-weekly-schedule.md's "Saturday -- contingency check" section for
the full spec this module implements.

**What Saturday can actually read.** Sleeper (`sleeper-daily`, daily) and
nflverse (`nflverse-routine`, daily) both run Saturday; ESPN's last pull was
Wednesday, so roster and lineup moves made Thursday through Saturday are
invisible; no odds job runs Saturday at all, and this report must not imply
one did.

**The baseline comes from Sleeper snapshots only.** `data/sleeper/slim/`
keeps 10 daily snapshots, so Friday's row for every player is on disk
Saturday. nflverse's `injuries` parquet is overwritten in place by each pull
and nflverse publishes no history, so Friday's official designation is
unrecoverable -- `report_status` below is rendered as today's absolute word,
labelled undiffable, not diffed against Friday.

Reuse-first, like every other day module: `wednesday.is_at_risk`,
`wednesday.best_replacement`, `wednesday.practice_signals`/
`depth_chart_moves`, `availability.read`/`sleeper_by_espn_id`,
`tuesday._bench`/`slot_map_divergence`, `monday.league_slots`,
`schedule.remaining_games`/`teams_in`, and `sleeper_signals.availability_tier`
computed directly against two snapshots rather than a second tier function.
"""

import time

import pandas as pd

from .. import config, weeks
from ..sleeper import signals as sleeper_signals
from . import availability, monday, payload as payload_lib, schedule, tuesday, wednesday
from .loaders import espn_export_warning, freshness, latest_export
from .render import INSUFFICIENT_DATA, freshness_lines, header_lines, table

# A module-local severity ranking, used only to decide whether a tier move
# reads "worse" or "better" -- sleeper_signals.availability_tier defines no
# ordering of its own, since trending/tier are meant to be read, not ranked.
_TIER_SEVERITY = {
    "CLEAR": 0,
    "LIKELY_PLAYS": 1,
    "UNKNOWN": 2,
    "COIN_FLIP": 3,
    "HIGH_RISK": 4,
    "OUT": 5,
}


def baseline_snapshot(today, snapshots_by_date=None):
    """{"insufficient", "reason", "baseline_date", "current_date",
    "days_used"} -- the same gate shape as friday.line_movement_read /
    thursday.props_read / waivers.team_totals.

    A reason-string ladder: no slim snapshots on disk at all; the newest
    slim date is not today's ET date (today's Sleeper run has not landed --
    this names the actual newest date rather than rendering a degenerate
    self-diff); only one snapshot exists, so there is no prior day to
    compare against. `baseline_date` is the newest date strictly before
    today's, and `days_used` reports the gap actually used -- following
    `sleeper_signals.depth_chart_delta`'s existing convention -- rather than
    assuming it is always exactly one day."""
    snapshots_by_date = wednesday._snapshots_by_date() if snapshots_by_date is None else snapshots_by_date
    empty = {"insufficient": True, "baseline_date": None, "current_date": None, "days_used": None}
    if not snapshots_by_date:
        return {**empty, "reason": "no Sleeper slim snapshots on disk"}

    dates = sorted(snapshots_by_date)
    newest = dates[-1]
    if newest != today:
        return {
            **empty,
            "reason": f"today's Sleeper snapshot has not landed -- the newest on disk is {newest}",
        }

    prior_dates = [d for d in dates if d < today]
    if not prior_dates:
        return {
            **empty, "current_date": today,
            "reason": f"only one Sleeper snapshot exists ({today}) -- no prior day to compare against",
        }

    baseline_date = max(prior_dates)
    return {
        "insufficient": False, "reason": None,
        "baseline_date": baseline_date, "current_date": today,
        "days_used": (today - baseline_date).days,
    }


def _by_sleeper_id(day_df):
    if day_df is None or day_df.empty:
        return {}
    return {r["sleeper_id"]: r for _, r in day_df.iterrows()}


def tier_diff(week_rosters, baseline_df, current_df, sleeper_df):
    """Per-player `sleeper_signals.availability_tier`, computed against both
    snapshots directly -- reusing that function as-is rather than defining a
    second one, the standing rule in `availability.py`'s docstring.

    Returns (rows, unmatched_names). A player with no Sleeper id match, or
    no row in one of the two snapshots (e.g. added to the roster since the
    baseline day), is counted and named in `unmatched_names` rather than
    read as unchanged -- the same counted-and-named convention
    `wednesday.best_replacement`'s `nan_names` uses. Every remaining row is
    returned, changed or not; callers filter to `changed` rows for display."""
    if week_rosters.empty:
        return [], set()

    baseline_by_id = _by_sleeper_id(baseline_df)
    current_by_id = _by_sleeper_id(current_df)

    rows, unmatched_names = [], set()
    for _, player in week_rosters.iterrows():
        player_id = player["player_id"]
        match = sleeper_df[sleeper_df["espn_player_id"] == player_id] if not sleeper_df.empty else pd.DataFrame()
        if match.empty:
            unmatched_names.add(player["player_name"])
            continue

        sleeper_id = match.iloc[0]["sleeper_id"]
        then_row, now_row = baseline_by_id.get(sleeper_id), current_by_id.get(sleeper_id)
        if then_row is None or now_row is None:
            unmatched_names.add(player["player_name"])
            continue

        tier_then = sleeper_signals.availability_tier(then_row.get("injury_status"), then_row.get("practice_participation"))
        tier_now = sleeper_signals.availability_tier(now_row.get("injury_status"), now_row.get("practice_participation"))
        changed = tier_then != tier_now
        direction = None
        if changed:
            then_sev = _TIER_SEVERITY.get(tier_then, 0)
            now_sev = _TIER_SEVERITY.get(tier_now, 0)
            direction = "worse" if now_sev > then_sev else "better"

        rows.append({
            "player_id": player_id, "player_name": player["player_name"],
            "tier_then": tier_then, "tier_now": tier_now, "changed": changed, "direction": direction,
            "replacement": None,
        })
    return rows, unmatched_names


def status_changes(week_rosters, sleeper_df, baseline_df, current_df):
    """Raw Sleeper `status`/`active` transitions between the two snapshots
    (both columns are in `sleeper_snapshots.SLIM_COLUMNS`) -- a
    `Practice Squad` -> `Active` elevation is the signal this section exists
    for, but every raw transition is reported rather than interpreted."""
    if week_rosters.empty:
        return []

    baseline_by_id = _by_sleeper_id(baseline_df)
    current_by_id = _by_sleeper_id(current_df)

    rows = []
    for _, player in week_rosters.iterrows():
        player_id = player["player_id"]
        match = sleeper_df[sleeper_df["espn_player_id"] == player_id] if not sleeper_df.empty else pd.DataFrame()
        if match.empty:
            continue

        sleeper_id = match.iloc[0]["sleeper_id"]
        then_row, now_row = baseline_by_id.get(sleeper_id), current_by_id.get(sleeper_id)
        if then_row is None or now_row is None:
            continue

        status_then, status_now = then_row.get("status"), now_row.get("status")
        active_then, active_now = then_row.get("active"), now_row.get("active")
        if status_then == status_now and active_then == active_now:
            continue
        rows.append({
            "player_name": player["player_name"],
            "status_then": status_then, "status_now": status_now,
            "active_then": active_then, "active_now": active_now,
        })
    return rows


def depth_chart_moves_since(week_rosters, today):
    """1-day-lookback depth-chart moves, via `wednesday.practice_signals`'
    existing `lookback_days` parameter and the existing
    `wednesday.depth_chart_moves` -- Saturday just passes 1 instead of
    `wednesday.DEPTH_LOOKBACK_DAYS`."""
    if week_rosters.empty:
        return []
    roster_names = dict(zip(week_rosters["player_id"], week_rosters["player_name"]))
    signals_df = wednesday.practice_signals(week_rosters, today=today, lookback_days=1)
    return wednesday.depth_chart_moves(signals_df, roster_names)


def bye_week_starters(starters, season, week):
    """Starters whose `pro_team` is in no game this week at all -- needs the
    whole week's slate, via `schedule.remaining_games(weekday=None)`, not one
    weekday's."""
    if starters.empty:
        return []
    games = schedule.remaining_games(season, week, weekday=None)
    teams_playing = schedule.teams_in(games)
    if not teams_playing:
        return []
    on_bye = starters[~starters["pro_team"].isin(teams_playing)]
    return [{"player_name": r["player_name"], "pro_team": r.get("pro_team")} for _, r in on_bye.iterrows()]


def parked_in_ir(week_rosters, avail_by_id):
    """Players sitting in an `IR` lineup slot whose resolved tier is not
    at-risk (`wednesday.is_at_risk`) -- someone occupying an IR slot who
    could be scoring. The inverse of `tuesday.ir_eligible`."""
    if week_rosters.empty:
        return []
    ir_rows = week_rosters[week_rosters["lineup_slot"] == "IR"]
    rows = []
    for _, r in ir_rows.iterrows():
        tier = avail_by_id.get(r["player_id"])
        if wednesday.is_at_risk(tier):
            continue
        rows.append({"player_name": r["player_name"], "tier": tier or INSUFFICIENT_DATA})
    return rows


def swap_pairs(diff_rows, starters, bench, pool_df, allowed_slots, avail_by_id):
    """For each starter whose tier moved worse, the best legal bench
    replacement via `wednesday.best_replacement` -- recomputed against
    today's data rather than carried from Friday's artifact, since nothing
    persists Friday's computation. Returns (rows, fallback_names,
    nan_names); each row is its matching `diff_rows` entry plus
    `replacement`."""
    if not diff_rows or starters.empty:
        return [], set(), set()

    starters_by_id = {r["player_id"]: r for _, r in starters.iterrows()}
    fallback_names, nan_names, rows = set(), set(), []
    for row in diff_rows:
        if not row["changed"] or row["direction"] != "worse":
            continue
        starter = starters_by_id.get(row["player_id"])
        if starter is None:
            continue
        replacement, fb, nan = wednesday.best_replacement(starter, bench, pool_df, allowed_slots, avail_by_id)
        fallback_names |= fb
        nan_names |= nan
        rows.append({**row, "replacement": replacement})
    return rows, fallback_names, nan_names


FOOTER_NOTES = [
    "nflverse publishes no history and its injuries parquet is overwritten in place by every pull -- "
    "Friday's official designation cannot be recovered, so only Sleeper's own movement is truly diffed "
    "above; \"Today's official designations\" is today's absolute nflverse read, not a diff against Friday.",
    "ESPN last pulled Wednesday (`espn-wednesday`) -- roster and lineup changes made Thursday through "
    "Saturday are invisible to this report.",
    "No odds job runs Saturday. Nothing here reflects market movement since Friday's `line_movement` "
    "capture.",
    "Replacements above are recomputed against today's data, not carried from Friday's lineup-lock "
    "artifact -- the two agree whenever no input has moved since Friday.",
    "Official inactives drop roughly 90 minutes before kickoff and appear in no feed this pipeline "
    "touches.",
    "This filename carries the render date, not the covered one, same convention as every other report.",
]


def render(season, week, team_id, gate, diff_rows, unmatched_names, status_rows, depth_moves,
           bye_starters, parked_rows, official_rows, footer_notes, window=None, rendered_at=None):
    """Pure over its arguments -- no disk access beyond the freshness
    header, same contract as every other day module."""
    rendered_at = rendered_at if rendered_at is not None else time.time()
    title = f"Contingency check -- {season} week {week}"
    covers = f"week {week}'s Saturday contingency check"

    lines = header_lines(title, week, covers, window, rendered_at)
    lines.append("")

    lines.append("## Freshness")
    fresh = {name: value for name, value in freshness(season=season).items() if name != "odds"}
    lines.extend(freshness_lines(fresh))
    lines.append("")

    lines.append("## Decisions due")
    lines.append("Hold or adjust a single slot.")
    lines.append("")
    if bye_starters:
        lines.append(f"{len(bye_starters)} starter(s) are on a bye this week:")
        lines.extend(f"- {r['player_name']} ({r['pro_team']})" for r in bye_starters)
    else:
        lines.append("No starter is on a bye this week.")
    lines.append("")
    if parked_rows:
        lines.append(
            f"{len(parked_rows)} player(s) are sitting in an IR slot but are not at-risk -- "
            "they could be scoring:"
        )
        lines.extend(f"- {r['player_name']} (tier={r['tier']})" for r in parked_rows)
    else:
        lines.append("No healthy player is parked in an IR slot.")
    lines.append("")

    baseline_label = gate["baseline_date"] if gate["baseline_date"] is not None else "--"
    lines.append(f"## Tier changes since {baseline_label}")
    changed = [r for r in diff_rows if r["changed"]]
    if gate["insufficient"]:
        lines.append(f"**{INSUFFICIENT_DATA}** -- {gate['reason']}")
    elif not changed:
        lines.append(f"No tier changed since {gate['baseline_date']}.")
    else:
        headers = ["player", "then", "now", "direction", "best replacement"]
        rows = []
        for r in changed:
            repl = r.get("replacement")
            if repl:
                repl_text = repl["player_name"]
            elif r["direction"] == "worse":
                repl_text = "no legal swap"
            else:
                repl_text = "--"
            rows.append([r["player_name"], r["tier_then"], r["tier_now"], r["direction"], repl_text])
        lines.extend(table(headers, rows))
    lines.append("")

    lines.append("## Roster status and depth chart")
    lines.append("### Practice-squad elevations")
    if not status_rows:
        lines.append("No status/active transition since the baseline snapshot.")
    else:
        headers = ["player", "status then", "status now", "active then", "active now"]
        rows = [
            [r["player_name"], r["status_then"], r["status_now"], r["active_then"], r["active_now"]]
            for r in status_rows
        ]
        lines.extend(table(headers, rows))
    lines.append("")
    lines.append("### Depth chart moves (1-day lookback)")
    if not depth_moves:
        lines.append("No rostered player's Sleeper depth-chart order improved since yesterday.")
    else:
        headers = ["player", "current order", "improved", "promoted", "days used"]
        rows = [
            [m["player_name"], m["depth_chart_order"], m["improved"], m["promoted"], m["days_used"]]
            for m in depth_moves
        ]
        lines.extend(table(headers, rows))
    lines.append("")

    lines.append("## Today's official designations")
    if not official_rows:
        lines.append("No at-risk starter carries an nflverse `report_status` today.")
    else:
        headers = ["player", "slot", "report_status"]
        rows = [
            [r["player_name"], r["lineup_slot"], r["nflverse_report_status"] or INSUFFICIENT_DATA]
            for r in official_rows
        ]
        lines.extend(table(headers, rows))
    lines.append("")

    lines.append("## What this report cannot see")
    lines.extend(f"- {n}" for n in footer_notes)
    return "\n".join(lines) + "\n"


def payload(season, week, team_id, gate, diff_rows, unmatched_names, status_rows, depth_moves,
            bye_starters, parked_rows, official_rows, footer_notes, window=None, rendered_at=None):
    """The structured twin of `render`, over the identical argument list.

    Note this report's freshness block drops `odds` -- no odds job runs on a
    Saturday -- and that `## Tier changes since <date>` builds its heading at
    render time, which is why the section id is a fixed string. See
    espn_ff/report/payload.py.
    """
    rendered_at = rendered_at if rendered_at is not None else time.time()
    title = f"Contingency check -- {season} week {week}"
    covers = f"week {week}'s Saturday contingency check"

    header = payload_lib.header_block(title, week, covers, window, rendered_at)
    fresh = {name: value for name, value in freshness(season=season).items() if name != "odds"}

    sections = [
        payload_lib.freshness_section(fresh),
        _decisions_section(bye_starters, parked_rows),
        _tier_changes_section(gate, diff_rows),
        _roster_status_section(status_rows, depth_moves),
    ]

    if not official_rows:
        sections.append(payload_lib.prose_section(
            "official-designations", "Today's official designations",
            ["No at-risk starter carries an nflverse `report_status` today."], data={"count": 0},
        ))
    else:
        columns = [
            payload_lib.column("player_name", "player", "string"),
            payload_lib.column("lineup_slot", "slot", "string"),
            payload_lib.column("nflverse_report_status", "report_status", "string"),
        ]
        sections.append(payload_lib.table_section(
            "official-designations", "Today's official designations", columns,
            payload_lib.rows(official_rows, columns), data={"count": len(official_rows)},
        ))

    sections.append(payload_lib.list_section("cannot-see", "What this report cannot see", footer_notes))
    return header, sections


def _decisions_section(bye_starters, parked_rows):
    """`## Decisions due` -- two lists the markdown renders as bullets: byes
    and healthy players parked in an IR slot. Both are actionable rosters, so
    they become tables rather than prose."""
    blocks = [payload_lib.prose_section(
        "decisions-due-lead", None, ["Hold or adjust a single slot."], level=None,
    )]

    if bye_starters:
        columns = [
            payload_lib.column("player_name", "player", "string"),
            payload_lib.column("pro_team", "pro_team", "string"),
        ]
        blocks.append(payload_lib.table_section(
            "bye-starters", None, columns, payload_lib.rows(bye_starters, columns),
            notes=[f"{len(bye_starters)} starter(s) are on a bye this week:"], level=None,
        ))
    else:
        blocks.append(payload_lib.prose_section(
            "bye-starters", None, ["No starter is on a bye this week."], level=None,
        ))

    if parked_rows:
        columns = [
            payload_lib.column("player_name", "player", "string"),
            payload_lib.column("tier", "tier", "string"),
        ]
        blocks.append(payload_lib.table_section(
            "parked-in-ir", None, columns, payload_lib.rows(parked_rows, columns),
            notes=[f"{len(parked_rows)} player(s) are sitting in an IR slot but are not at-risk -- "
                   "they could be scoring:"],
            level=None,
        ))
    else:
        blocks.append(payload_lib.prose_section(
            "parked-in-ir", None, ["No healthy player is parked in an IR slot."], level=None,
        ))

    return payload_lib.blocks_section(
        "decisions-due", "Decisions due", blocks,
        data={"bye_starter_count": len(bye_starters), "parked_in_ir_count": len(parked_rows)},
    )


def _tier_changes_section(gate, diff_rows):
    baseline_label = gate["baseline_date"] if gate["baseline_date"] is not None else "--"
    heading = f"Tier changes since {baseline_label}"
    data = {"baseline_date": gate["baseline_date"]}

    if gate["insufficient"]:
        return payload_lib.insufficient_section(
            "tier-changes", heading, gate["reason"], data=data,
        )
    changed = [r for r in diff_rows if r["changed"]]
    if not changed:
        return payload_lib.prose_section(
            "tier-changes", heading, [f"No tier changed since {gate['baseline_date']}."],
            data={**data, "count": 0},
        )

    columns = [
        payload_lib.column("player_name", "player", "string"),
        payload_lib.column("tier_then", "then", "string"),
        payload_lib.column("tier_now", "now", "string"),
        payload_lib.column("direction", "direction", "string"),
        payload_lib.column("replacement_name", "best replacement", "string"),
        payload_lib.column("no_legal_swap", "no legal swap", "boolean"),
    ]
    rows = []
    for r in changed:
        repl = r.get("replacement")
        rows.append({
            "player_name": payload_lib.unset(r["player_name"]),
            "tier_then": payload_lib.unset(r["tier_then"]),
            "tier_now": payload_lib.unset(r["tier_now"]),
            "direction": payload_lib.unset(r["direction"]),
            "replacement_name": payload_lib.unset(repl["player_name"]) if repl else None,
            # render collapses three states into one cell -- a name, the
            # words "no legal swap", or "--" for a tier that improved. The
            # boolean keeps "we looked and found nothing" apart from "we did
            # not need to look".
            "no_legal_swap": repl is None and r["direction"] == "worse",
        })
    return payload_lib.table_section(
        "tier-changes", heading, columns, rows, data={**data, "count": len(changed)},
    )


def _roster_status_section(status_rows, depth_moves):
    """`## Roster status and depth chart`, with two `###` groups."""
    if not status_rows:
        elevations = payload_lib.prose_section(
            "practice-squad-elevations", "Practice-squad elevations",
            ["No status/active transition since the baseline snapshot."], level=3,
            data={"count": 0},
        )
    else:
        columns = [
            payload_lib.column("player_name", "player", "string"),
            payload_lib.column("status_then", "status then", "string"),
            payload_lib.column("status_now", "status now", "string"),
            payload_lib.column("active_then", "active then", "boolean"),
            payload_lib.column("active_now", "active now", "boolean"),
        ]
        elevations = payload_lib.table_section(
            "practice-squad-elevations", "Practice-squad elevations", columns,
            payload_lib.rows(status_rows, columns), level=3, data={"count": len(status_rows)},
        )

    heading = "Depth chart moves (1-day lookback)"
    if not depth_moves:
        moves = payload_lib.prose_section(
            "depth-chart-moves", heading,
            ["No rostered player's Sleeper depth-chart order improved since yesterday."],
            level=3, data={"count": 0},
        )
    else:
        columns = [
            payload_lib.column("player_name", "player", "string"),
            payload_lib.column("depth_chart_order", "current order", "number"),
            payload_lib.column("improved", "improved", "boolean"),
            payload_lib.column("promoted", "promoted", "boolean"),
            payload_lib.column("days_used", "days used", "integer"),
        ]
        moves = payload_lib.table_section(
            "depth-chart-moves", heading, columns, payload_lib.rows(depth_moves, columns),
            level=3, data={"count": len(depth_moves)},
        )

    return payload_lib.blocks_section(
        "roster-status", "Roster status and depth chart", [elevations, moves],
    )


def build(season, week, team_id=None):
    """Assemble the full Saturday report as markdown text. `week` is the
    current scoring period with no offset, same contract as
    wednesday.build/friday.build."""
    team_id = team_id if team_id is not None else config.TEAM_ID
    rendered_at = time.time()
    today = pd.Timestamp(rendered_at, unit="s", tz="UTC").tz_convert(weeks.ET).date()

    rosters_df = latest_export("weekly-rosters")
    pool_df = latest_export("player-pool")
    roster_slots_df = latest_export("roster-slots")
    allowed_slots = monday.league_slots(roster_slots_df)

    week_rosters = (
        rosters_df[(rosters_df["week"] == week) & (rosters_df["team_id"] == team_id)]
        if not rosters_df.empty else rosters_df
    )
    starters = week_rosters[week_rosters["started"]] if not week_rosters.empty else week_rosters
    bench = tuesday._bench(week_rosters) if not week_rosters.empty else week_rosters

    avail_df = availability.read(week_rosters, season, week) if not week_rosters.empty else pd.DataFrame()
    avail_by_id = dict(zip(avail_df["player_id"], avail_df["tier"])) if not avail_df.empty else {}

    bye_starters = bye_week_starters(starters, season, week)
    parked_rows = parked_in_ir(week_rosters, avail_by_id)

    official_rows = []
    if not starters.empty and not avail_df.empty:
        avail_rows = {r["player_id"]: r for _, r in avail_df.iterrows()}
        for _, s in starters.iterrows():
            if not wednesday.is_at_risk(avail_by_id.get(s["player_id"])):
                continue
            source_row = avail_rows.get(s["player_id"], {})
            official_rows.append({
                "player_name": s["player_name"], "lineup_slot": s["lineup_slot"],
                "nflverse_report_status": source_row.get("nflverse_report_status"),
            })

    snapshots_by_date = wednesday._snapshots_by_date()
    gate = baseline_snapshot(today, snapshots_by_date=snapshots_by_date)

    diff_rows, unmatched_names, status_rows = [], set(), []
    fallback_names, nan_names = set(), set()
    if not gate["insufficient"] and not week_rosters.empty:
        baseline_df = snapshots_by_date.get(gate["baseline_date"])
        current_df = snapshots_by_date.get(gate["current_date"])
        sleeper_df = availability.sleeper_by_espn_id()

        diff_rows, unmatched_names = tier_diff(week_rosters, baseline_df, current_df, sleeper_df)
        status_rows = status_changes(week_rosters, sleeper_df, baseline_df, current_df)

        swap_rows, fallback_names, nan_names = swap_pairs(
            diff_rows, starters, bench, pool_df, allowed_slots, avail_by_id
        )
        swap_by_id = {r["player_id"]: r["replacement"] for r in swap_rows}
        for row in diff_rows:
            row["replacement"] = swap_by_id.get(row["player_id"])

    depth_moves = depth_chart_moves_since(week_rosters, today)

    footer_notes = list(FOOTER_NOTES)
    for name, (_, stale) in freshness(season=season).items():
        if name == "odds":
            continue
        if stale:
            footer_notes.append(f"The {name} feed is stale as of this report's generation.")
    export_warning = espn_export_warning()
    if export_warning:
        footer_notes.append(f"The last ESPN export shrank -- {export_warning}.")
    if gate["insufficient"]:
        footer_notes.append(f"The tier-change diff could not be read -- {gate['reason']}.")
    if unmatched_names:
        footer_notes.append(
            "No Sleeper match on both the baseline and current snapshot -- counted, not read as "
            "unchanged -- for: " + ", ".join(sorted(unmatched_names))
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
            f"{len(divergence_df)} player(s) in this week's pool have eligible_slots that diverge from "
            "the position-fallback map -- the fallback path may be wrong for them specifically."
        )

    args = (
        season, week, team_id, gate, diff_rows, unmatched_names, status_rows, depth_moves,
        bye_starters, parked_rows, official_rows, footer_notes,
    )
    kwargs = {"window": weeks.week_window(season, week), "rendered_at": rendered_at}
    header, sections = payload(*args, **kwargs)
    return payload_lib.RenderedReport(
        render(*args, **kwargs), {"header": header, "sections": sections}
    )
