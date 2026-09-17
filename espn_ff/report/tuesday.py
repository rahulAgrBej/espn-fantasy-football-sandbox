"""Tuesday -- the week-in-review report.

Two traps this module exists to avoid. First: `matchups.csv` is
cache-forever and un-ttl'd, so an unrefreshed week reads `winner ==
'UNDECIDED'` and `points_final == 0.0` exactly as it does mid-week on
Monday -- reporting that as a loss (or as a real 0-0 tie) is the single
error this report exists to prevent, so closure is judged defensively and
the unclosed state is first-class, not an afterthought. Second: the
`points` column silently falls back to live/in-progress scoring
(`extract/matchups.py`'s `_points()`), so once a game is no longer
in-progress `points` and `points_final` should agree -- but that has never
been Observed, only Inferred, since no week in this league has ever
closed. `points` is never read here; a result comes from `points_final`,
gated by closure, or not at all.

See docs/report-weekly-schedule.md's "Tuesday -- week in review" section
for the full spec this module implements.
"""

import time
from functools import lru_cache

import pandas as pd

from .. import config, weeks
from . import monday
from . import payload as payload_lib
from .loaders import freshness, latest_export, roster_staleness_note
from .render import freshness_lines, header_lines, num, table

_VALID_RESULTS = {"W", "L", "T"}

# eligible_slots on player-pool.csv is the primary path for slot
# eligibility. This map is the fallback for a player with no pool row at
# all (dropped since the reviewed week) -- derived from all 1,041 pool
# rows intersected with this league's actual slots, not guessed. TE is the
# trap: it looks flex-capable (raw eligibility carries WR/TE and FLEX) but
# this league rosters neither, and TE is not RB/WR-eligible while WR is.
_POSITION_SLOTS = {
    "QB": {"QB"},
    "RB": {"RB", "RB/WR"},
    "WR": {"RB/WR", "WR"},
    "TE": {"TE"},
    "K": {"K"},
    "D/ST": {"D/ST"},
}

# Dropping the sole backup at a one-deep position trades a real bye-week
# problem for a marginal add.
_ONE_DEEP_POSITIONS = {"QB", "TE", "K", "D/ST"}

_NON_STARTING_SLOTS = {"Bench", "IR"} | monday._IGNORED_LEAGUE_SLOTS


def _team_row(matchups_df, week, team_id):
    subset = matchups_df[(matchups_df["week"] == week) & (matchups_df["team_id"] == team_id)]
    return subset.iloc[0] if not subset.empty else None


def _derive_result(row):
    """Prefer the exported `result` column when it is a real W/L/T string;
    else re-derive from `winner` + `side`, mirroring
    extract/matchups.py's `_result`. `result` is float64 NaN until a week
    closes, so `row["result"] in ("W","L")` and `.strip()` both fail
    silently on today's data -- the column's dtype cannot be trusted."""
    value = row.get("result")
    if isinstance(value, str) and value in _VALID_RESULTS:
        return value
    winner, side = row.get("winner"), row.get("side")
    if winner == "TIE":
        return "T"
    if winner == "HOME":
        return "W" if side == "home" else "L"
    if winner == "AWAY":
        return "W" if side == "away" else "L"
    return None


def week_closed(matchups_df, week, team_id):
    """Whether `team_id`'s week-`week` matchup has actually closed.
    Follows monday.live_margin's {"insufficient": bool, "reason": str}
    convention, plus a `state` key diagnosing why -- the failure modes are
    not interchangeable: an unrefreshed week must never render as a loss.

    `winner` is the gate; `points_final` is only a coherence check. It has
    no open-state sentinel -- it means both "not landed" and "legitimately
    zero" -- so requiring `points_final > 0` for closure would hide a real,
    embarrassing loss as a data failure. The narrower, safe check: a
    decided game cannot legitimately be 0-0, so `winner` decided with
    *both* sides at 0.0 means the score half of the refresh did not land.

    Closure is judged per *game* (`groupby("matchup_id")`, open if either
    side reads UNDECIDED) -- "0 of N closed" (refresh failed) is a
    different message than "N-1 of N closed, ours isn't" (more alarming).

    `margin` is **ours - theirs** (positive = we won) -- the opposite of
    monday.live_margin's theirs - ours.
    """
    if matchups_df.empty:
        return {"insufficient": True, "state": "missing", "reason": "no matchups export on disk"}

    week_df = matchups_df[matchups_df["week"] == week]
    if week_df.empty:
        return {"insufficient": True, "state": "missing", "reason": f"no matchups rows for week {week}"}

    ours = _team_row(matchups_df, week, team_id)
    if ours is None:
        return {"insufficient": True, "state": "missing", "reason": "no matchup row for this team/week"}

    opponent_id, opponent_name = ours.get("opponent_id"), ours.get("opponent_name")
    if pd.isna(opponent_id):
        return {
            "insufficient": True, "state": "missing", "reason": "team has a bye this week (no opponent)",
            "opponent_id": None, "opponent_name": None,
        }

    def _game_open(rows):
        return (rows["winner"] == "UNDECIDED").any()

    game_id = ours.get("matchup_id")
    total_games = week_df["matchup_id"].nunique()
    closed_games = sum(1 for _, g in week_df.groupby("matchup_id") if not _game_open(g))
    our_game_open = _game_open(week_df[week_df["matchup_id"] == game_id])

    if closed_games == 0:
        return {
            "insufficient": True, "state": "unclosed",
            "reason": f"all {total_games} matchups still read UNDECIDED -- the ESPN refresh has not landed",
            "opponent_id": opponent_id, "opponent_name": opponent_name,
        }
    if our_game_open:
        return {
            "insufficient": True, "state": "partial",
            "reason": f"{closed_games} of {total_games} league matchups closed, ours is not one of them",
            "opponent_id": opponent_id, "opponent_name": opponent_name,
        }

    theirs = _team_row(matchups_df, week, opponent_id)
    our_final = ours.get("points_final")
    their_final = theirs.get("points_final") if theirs is not None else None

    if our_final == 0.0 and their_final == 0.0:
        return {
            "insufficient": True, "state": "incoherent",
            "reason": "winner is decided but both sides read points_final == 0.0 -- "
            "the score half of the refresh did not land",
            "opponent_id": opponent_id, "opponent_name": opponent_name,
        }

    partial_note = None
    if closed_games != total_games:
        partial_note = f"{closed_games} of {total_games} league matchups closed -- standings may be incomplete"

    return {
        "insufficient": False, "state": "closed",
        "result": _derive_result(ours),
        "our_points": our_final, "their_points": their_final,
        "margin": our_final - their_final,
        "opponent_id": opponent_id, "opponent_name": opponent_name,
        "partial_note": partial_note,
    }


def standings(teams_df, week):
    """`teams.csv` sorted by playoff_seed, flagged insufficient when every
    team still reads the pre-season snapshot (wins == losses == 0 and
    points_for == 0.0) -- which must not render as a real 12-way tie."""
    if teams_df.empty:
        return {"insufficient": True, "reason": "no teams export on disk"}

    all_zero = ((teams_df["wins"] == 0) & (teams_df["losses"] == 0) & (teams_df["points_for"] == 0.0)).all()
    if all_zero:
        return {
            "insufficient": True,
            "reason": "every team still reads the pre-season snapshot (0-0, 0.0 points_for)",
        }

    sorted_df = teams_df.sort_values("playoff_seed", na_position="last").reset_index(drop=True)
    games_note = None
    mismatched = sorted_df[(sorted_df["wins"] + sorted_df["losses"] + sorted_df["ties"]) != week]
    if not mismatched.empty:
        games_note = (
            f"{len(mismatched)} team(s) whose wins+losses+ties != week {week} -- "
            "check against the closure gate"
        )
    return {"insufficient": False, "rows": sorted_df, "games_note": games_note}


def _bench(rosters_df):
    """Bench players, excluding IR -- `started == False` alone includes IR
    rows, so this filter must be explicit."""
    return rosters_df[(~rosters_df["started"]) & (rosters_df["lineup_slot"] != "IR")]


def _eligible_slots(player_id, position, pool_df, allowed_slots):
    """monday.eligible_slots_for is the primary path; on an empty result
    (a player dropped since the reviewed week has no pool row at all) fall
    back to the position map intersected with this league's slots. Returns
    (slots, used_fallback)."""
    slots = monday.eligible_slots_for(player_id, pool_df, allowed_slots)
    if slots:
        return slots, False
    return _POSITION_SLOTS.get(position, set()) & allowed_slots, True


def slot_map_divergence(pool_df, allowed_slots):
    """Players whose actual *starting-slot* eligibility (eligible_slots
    intersected with this league's slots, minus Bench/IR -- every rostered
    player is trivially Bench/IR-eligible, so that part of the comparison
    is meaningless) disagrees with _POSITION_SLOTS' pure-position
    derivation -- today this is empty across all 1,041 pool rows, but it
    is an each-run check, not a standing assumption. Parses eligible_slots
    directly rather than through monday.eligible_slots_for, which
    re-filters pool_df per call and would make this O(n^2) over the full
    pool."""
    columns = ["player_id", "player_name", "position", "actual", "position_derived"]
    if pool_df.empty:
        return pd.DataFrame(columns=columns)

    starting_slots = allowed_slots - {"Bench", "IR"}
    rows = []
    for row in pool_df.itertuples():
        raw = getattr(row, "eligible_slots", "")
        actual = ({s.strip() for s in str(raw or "").split(",") if s.strip()} & allowed_slots) - {"Bench", "IR"}
        derived = _POSITION_SLOTS.get(row.position, set()) & starting_slots
        if actual != derived:
            rows.append(
                {
                    "player_id": row.player_id, "player_name": row.player_name, "position": row.position,
                    "actual": sorted(actual), "position_derived": sorted(derived),
                }
            )
    return pd.DataFrame(rows, columns=columns)


def regret_table(rosters_df, week, team_id, pool_df, allowed_slots):
    """Per-starter regret: the highest-scoring bench player eligible for
    that starter's slot who outscored them. Independent per starter --
    deliberately no assignment constraint, since each row is a
    self-contained "was there a better player for this slot" counterfactual.
    The same bench player may legitimately answer more than one row; the
    gap column must not be summed -- see optimal_lineup for the one number
    that does. NaN points are partitioned out (named, not compared), since
    every comparison against NaN is False and would otherwise render an
    unscored player as fine by omission.

    Returns (rows, fallback_names, nan_names)."""
    if rosters_df.empty:
        return [], set(), set()

    week_rosters = rosters_df[(rosters_df["week"] == week) & (rosters_df["team_id"] == team_id)]
    starters = week_rosters[week_rosters["started"]]
    bench = _bench(week_rosters)

    fallback_names, nan_names, rows = set(), set(), []

    for _, starter in starters.iterrows():
        if pd.isna(starter["points"]):
            nan_names.add(starter["player_name"])
            continue
        target_slot = starter["lineup_slot"]

        best = None
        for _, bp in bench.iterrows():
            if pd.isna(bp["points"]):
                nan_names.add(bp["player_name"])
                continue
            bp_slots, bp_fallback = _eligible_slots(bp["player_id"], bp["position"], pool_df, allowed_slots)
            if bp_fallback:
                fallback_names.add(bp["player_name"])
            if target_slot not in bp_slots or bp["points"] <= starter["points"]:
                continue
            if best is None or bp["points"] > best["points"]:
                best = bp

        if best is None:
            continue
        rows.append(
            {
                "slot": target_slot,
                "starter_name": starter["player_name"], "starter_points": starter["points"],
                "bench_name": best["player_name"], "bench_points": best["points"],
                "gap": best["points"] - starter["points"],
            }
        )

    rows.sort(key=lambda r: r["gap"], reverse=True)
    return rows, fallback_names, nan_names


def _slot_instances(roster_slots_df):
    """Expand roster-slots.csv into one entry per starting-slot instance --
    9 for a QB1/RB2/RB-WR1/WR2/TE1/D-ST1/K1 league. Bench/IR/FLEX/OP are
    never legal DP targets."""
    slots = []
    for _, row in roster_slots_df.iterrows():
        if row["slot"] in _NON_STARTING_SLOTS:
            continue
        slots.extend([row["slot"]] * int(row["count"]))
    return slots


def solve_slots(values, eligibility, slot_list):
    """Exact maximum-value assignment of candidates to starting-slot
    instances. `values[i]` and `eligibility[i]` are parallel, one entry per
    candidate; `slot_list` is _slot_instances' output. Returns
    (total, ordered_slots, chosen) where `chosen[i]` indexes into `values`
    for `ordered_slots[i]`, or -1 when no eligible candidate remained.
    Callers map indices back to their own rows -- this function knows
    nothing about points, projections, rosters or IR.

    Bitmask DP over candidates, memoized on (slot_index, used_mask), slots
    ordered most-constrained (fewest eligible candidates) first to keep
    branching low. Roughly 40k states worst case for 9 slots and a dozen
    startable candidates -- instant, and no new dependency (scipy/networkx
    are not installed)."""
    counts = [sum(1 for e in eligibility if slot in e) for slot in slot_list]
    ordered_slots = [slot_list[i] for i in sorted(range(len(slot_list)), key=lambda i: counts[i])]

    @lru_cache(maxsize=None)
    def solve(slot_index, used_mask):
        if slot_index == len(ordered_slots):
            return 0.0, ()
        slot = ordered_slots[slot_index]
        best_value, best_choice = None, None
        for i, value in enumerate(values):
            if used_mask & (1 << i) or slot not in eligibility[i]:
                continue
            rest_value, rest_choice = solve(slot_index + 1, used_mask | (1 << i))
            total = value + rest_value
            if best_value is None or total > best_value:
                best_value, best_choice = total, (i,) + rest_choice
        if best_choice is None:
            # No eligible candidate remains for this slot -- shouldn't happen
            # against a full roster, but skip rather than crash.
            rest_value, rest_choice = solve(slot_index + 1, used_mask)
            return rest_value, (-1,) + rest_choice
        return best_value, best_choice

    total, chosen = solve(0, 0)
    solve.cache_clear()
    return total, ordered_slots, list(chosen)


def optimal_lineup(rosters_df, week, team_id, pool_df, roster_slots_df, allowed_slots):
    """Exact best legal lineup, via solve_slots over this week's rosterable
    (non-IR) players scored on `points`.

    Returns None (headline suppressed) when any rosterable (non-IR)
    player's points reads NaN -- every comparison against NaN is False, so
    an unhandled NaN would silently render as optimal."""
    if rosters_df.empty or roster_slots_df.empty:
        return None

    week_rosters = rosters_df[(rosters_df["week"] == week) & (rosters_df["team_id"] == team_id)]
    rosterable = week_rosters[week_rosters["lineup_slot"] != "IR"]
    if rosterable.empty or rosterable["points"].isna().any():
        return None

    candidates = list(rosterable.itertuples())
    fallback_names = set()
    eligibility = []
    for c in candidates:
        slots, used_fallback = _eligible_slots(c.player_id, c.position, pool_df, allowed_slots)
        if used_fallback:
            fallback_names.add(c.player_name)
        eligibility.append(slots)

    slot_list = _slot_instances(roster_slots_df)
    values = [c.points for c in candidates]
    optimal_points, ordered_slots, chosen = solve_slots(values, eligibility, slot_list)

    lineup = [
        {"slot": ordered_slots[i], "player_name": candidates[idx].player_name, "points": candidates[idx].points}
        for i, idx in enumerate(chosen)
        if idx != -1
    ]
    actual_points = rosterable.loc[rosterable["started"], "points"].sum()

    return {
        "optimal_points": optimal_points,
        "actual_points": actual_points,
        "left_on_table": optimal_points - actual_points,
        "lineup": lineup,
        "fallback_names": fallback_names,
    }


def drop_candidates(rosters_df, pool_df, week, team_id):
    """Bench players ranked by rest-of-season projection ascending,
    excluding the sole backup at a one-deep position (QB/TE/K/D-ST) --
    dropping that player trades a real bye-week problem for a marginal
    add. ROS projection is Inferred as season_projected - season_points,
    since player-pool.csv has no ROS column; a bench player with no pool
    row renders insufficient data rather than a zero."""
    if rosters_df.empty:
        return []

    week_rosters = rosters_df[(rosters_df["week"] == week) & (rosters_df["team_id"] == team_id)]
    bench = _bench(week_rosters)
    bench_position_counts = bench["position"].value_counts()

    rows = []
    for _, bp in bench.iterrows():
        position = bp["position"]
        if position in _ONE_DEEP_POSITIONS and bench_position_counts.get(position, 0) == 1:
            continue
        pool_row = pool_df[pool_df["player_id"] == bp["player_id"]] if not pool_df.empty else pd.DataFrame()
        ros = None
        if not pool_row.empty:
            ros = pool_row["season_projected"].iloc[0] - pool_row["season_points"].iloc[0]
        rows.append({"player_name": bp["player_name"], "position": position, "ros_projection": ros})

    rows.sort(key=lambda r: (r["ros_projection"] is None, r["ros_projection"]))
    return rows


def ir_eligible(rosters_df, week, team_id):
    """Roster rows carrying ESPN's own INJURY_RESERVE designation that
    aren't already sitting in an IR slot. Whether OUT/DOUBTFUL also
    qualifies is a league setting recorded in no artifact here, so those
    are returned separately under a stated assumption rather than
    asserted as eligible."""
    if rosters_df.empty:
        return pd.DataFrame(columns=rosters_df.columns), pd.DataFrame(columns=rosters_df.columns)

    week_rosters = rosters_df[(rosters_df["week"] == week) & (rosters_df["team_id"] == team_id)]
    now = week_rosters[(week_rosters["injury_status"] == "INJURY_RESERVE") & (week_rosters["lineup_slot"] != "IR")]
    maybe = week_rosters[week_rosters["injury_status"].isin(["OUT", "DOUBTFUL"])]
    return now, maybe


FOOTER_NOTES = [
    "nflverse's provisional flag is still true on Tuesday -- stat corrections land Tuesday and "
    "Wednesday, so these point totals may themselves move.",
    "This regret table is retrospective and explicitly not a start/sit rule -- one week of outcome "
    "tells you less than Friday's projection will.",
    "Per-slot regret gaps are independent counterfactuals and do not add; see \"points left on the "
    "table\" for the one number that does.",
    "A 0.0 in points does not distinguish played-and-scored-zero from a bye from inactive.",
    "This league processes waivers Tuesday night into Wednesday (Documented -- league setting, per "
    "the league manager); the IR-eligibility guidance above is anchored to that rule.",
]


def render(
    season, review_week, closure, standings_result, regret_rows, optimal, drops, ir_now, ir_maybe, footer_notes,
    window=None, rendered_at=None,
):
    """`window` is `(start_et, end_et)` for `review_week`, from
    `espn_ff.weeks.week_window` -- passed in rather than looked up here so
    this stays a pure function over its fixtures, with no disk access."""
    rendered_at = rendered_at if rendered_at is not None else time.time()
    title = f"Week {review_week} in review -- {season}"
    covers = f"week {review_week}, the completed week this report reviews"

    lines = header_lines(title, review_week, covers, window, rendered_at)
    lines.append("")

    lines.append("## Freshness")
    lines.extend(freshness_lines(freshness(season=season)))
    lines.append("")

    lines.append("## Decisions due")
    lines.append(
        "None binding today. This report seeds the shortlist for the 11:00 waiver-wire report."
    )
    if not ir_now.empty:
        lines.append("")
        lines.append("IR-eligible now:")
        lines.extend(f"- {r['player_name']} ({r['position']}) -- INJURY_RESERVE" for _, r in ir_now.iterrows())
    if not ir_maybe.empty:
        lines.append("")
        lines.append("Possibly IR-eligible, depending on this league's setting (assumption, unconfirmed):")
        lines.extend(f"- {r['player_name']} ({r['position']}) -- {r['injury_status']}" for _, r in ir_maybe.iterrows())
    lines.append("")

    lines.append("## Result")
    if closure["insufficient"]:
        lines.append(f"**{closure['state']} -- insufficient data** -- {closure['reason']}")
    else:
        verb = {"W": "won", "L": "lost", "T": "tied"}.get(closure["result"], "result unclear")
        lines.append(
            f"{verb.capitalize()}. Us {num(closure['our_points'])} -- "
            f"{closure.get('opponent_name', 'opponent')} {num(closure['their_points'])} "
            f"(margin {num(closure['margin'])}, ours minus theirs)"
        )
        if closure.get("partial_note"):
            lines.append(f"_{closure['partial_note']}_")
    lines.append("")

    lines.append("## Standings")
    if standings_result["insufficient"]:
        lines.append(f"**insufficient data** -- {standings_result['reason']}")
    else:
        headers = ["seed", "team", "record", "points for"]
        rows = [
            [r["playoff_seed"], r["team_name"], f"{r['wins']}-{r['losses']}-{r['ties']}", num(r["points_for"])]
            for _, r in standings_result["rows"].iterrows()
        ]
        lines.extend(table(headers, rows))
        if standings_result.get("games_note"):
            lines.append("")
            lines.append(f"_{standings_result['games_note']}_")
    lines.append("")

    lines.append("## Optimal-lineup regret")
    if optimal is None:
        lines.append(
            "**insufficient data** -- at least one rostered player's points read NaN this week; "
            "the optimal-lineup headline is suppressed rather than computed against a missing score."
        )
    else:
        lines.append(
            f"**{num(optimal['left_on_table'])} points left on the table** "
            f"(optimal {num(optimal['optimal_points'])} vs actual {num(optimal['actual_points'])})."
        )
    lines.append("")
    lines.append(
        "_Per-slot gaps below are independent counterfactuals and do not add; for the one number "
        "that does, see \"points left on the table\" above._"
    )
    lines.append("")
    if regret_rows:
        headers = ["slot", "started", "pts", "best bench", "pts", "gap"]
        rows = [
            [
                r["slot"], r["starter_name"], num(r["starter_points"]),
                r["bench_name"], num(r["bench_points"]), num(r["gap"]),
            ]
            for r in regret_rows
        ]
        lines.extend(table(headers, rows))
    else:
        lines.append("No starter was outscored by an eligible bench player this week.")
    lines.append("")

    lines.append("## Drop candidates")
    if drops:
        headers = ["player", "position", "ROS projection (inferred)"]
        rows = [[d["player_name"], d["position"], num(d["ros_projection"])] for d in drops]
        lines.extend(table(headers, rows))
    else:
        lines.append("No bench player is a drop candidate this week.")
    lines.append("")

    lines.append("## What this report cannot see")
    lines.extend(f"- {n}" for n in footer_notes)
    return "\n".join(lines) + "\n"


def payload(
    season, review_week, closure, standings_result, regret_rows, optimal, drops, ir_now, ir_maybe, footer_notes,
    window=None, rendered_at=None,
):
    """The structured twin of `render`, over the identical argument list. See
    espn_ff/report/payload.py."""
    rendered_at = rendered_at if rendered_at is not None else time.time()
    title = f"Week {review_week} in review -- {season}"
    covers = f"week {review_week}, the completed week this report reviews"

    header = payload_lib.header_block(title, review_week, covers, window, rendered_at)
    sections = [payload_lib.freshness_section(freshness(season=season))]

    ir_columns = [
        payload_lib.column("player_name", "player", "string"),
        payload_lib.column("position", "position", "string"),
        payload_lib.column("status", "status", "string"),
    ]
    decisions_blocks = [payload_lib.prose_section(
        "decisions-due-lead", None,
        ["None binding today. This report seeds the shortlist for the 11:00 waiver-wire report."],
        level=None,
    )]
    if not ir_now.empty:
        decisions_blocks.append(payload_lib.table_section(
            "ir-eligible-now", None, ir_columns,
            [{"player_name": payload_lib.unset(r["player_name"]),
              "position": payload_lib.unset(r["position"]), "status": "INJURY_RESERVE"}
             for _, r in ir_now.iterrows()],
            notes=["IR-eligible now:"], level=None,
        ))
    if not ir_maybe.empty:
        decisions_blocks.append(payload_lib.table_section(
            "ir-eligible-maybe", None, ir_columns,
            [{"player_name": payload_lib.unset(r["player_name"]),
              "position": payload_lib.unset(r["position"]),
              "status": payload_lib.unset(r["injury_status"])}
             for _, r in ir_maybe.iterrows()],
            notes=["Possibly IR-eligible, depending on this league's setting "
                   "(assumption, unconfirmed):"],
            level=None,
        ))
    sections.append(payload_lib.blocks_section(
        "decisions-due", "Decisions due", decisions_blocks,
        data={"binding": False, "ir_now_count": len(ir_now), "ir_maybe_count": len(ir_maybe)},
    ))

    if closure["insufficient"]:
        sections.append(payload_lib.insufficient_section(
            "result", "Result", closure["reason"],
            data={"state": closure["state"]},
        ))
    else:
        verb = {"W": "won", "L": "lost", "T": "tied"}.get(closure["result"], "result unclear")
        body = [
            f"{verb.capitalize()}. Us {num(closure['our_points'])} -- "
            f"{closure.get('opponent_name', 'opponent')} {num(closure['their_points'])} "
            f"(margin {num(closure['margin'])}, ours minus theirs)"
        ]
        if closure.get("partial_note"):
            body.append(f"_{closure['partial_note']}_")
        sections.append(payload_lib.prose_section(
            "result", "Result", body,
            data={
                "result": closure["result"], "our_points": closure["our_points"],
                "their_points": closure["their_points"], "margin": closure["margin"],
                "opponent_name": closure.get("opponent_name"), "state": closure.get("state"),
                "partial_note": closure.get("partial_note"),
            },
        ))

    if standings_result["insufficient"]:
        sections.append(payload_lib.insufficient_section(
            "standings", "Standings", standings_result["reason"],
        ))
    else:
        columns = [
            payload_lib.column("playoff_seed", "seed", "integer"),
            payload_lib.column("team_name", "team", "string"),
            payload_lib.column("record", "record", "string"),
            payload_lib.column("wins", "wins", "integer"),
            payload_lib.column("losses", "losses", "integer"),
            payload_lib.column("ties", "ties", "integer"),
            payload_lib.column("points_for", "points for", "number"),
        ]
        # `record` is the markdown's "W-L-T" string; wins/losses/ties are
        # carried beside it so a consumer can sort on them without parsing.
        rows = [
            {
                "playoff_seed": payload_lib.unset(r["playoff_seed"]),
                "team_name": payload_lib.unset(r["team_name"]),
                "record": f"{r['wins']}-{r['losses']}-{r['ties']}",
                "wins": payload_lib.unset(r["wins"]), "losses": payload_lib.unset(r["losses"]),
                "ties": payload_lib.unset(r["ties"]),
                "points_for": payload_lib.unset(r["points_for"]),
            }
            for _, r in standings_result["rows"].iterrows()
        ]
        notes = []
        if standings_result.get("games_note"):
            notes.append(f"_{standings_result['games_note']}_")
        sections.append(payload_lib.table_section("standings", "Standings", columns, rows, notes=notes))

    sections.append(_regret_section(optimal, regret_rows))

    if drops:
        columns = [
            payload_lib.column("player_name", "player", "string"),
            payload_lib.column("position", "position", "string"),
            payload_lib.column("ros_projection", "ROS projection (inferred)", "number"),
        ]
        sections.append(payload_lib.table_section(
            "drop-candidates", "Drop candidates", columns, payload_lib.rows(drops, columns),
            data={"count": len(drops)},
        ))
    else:
        sections.append(payload_lib.prose_section(
            "drop-candidates", "Drop candidates",
            ["No bench player is a drop candidate this week."], data={"count": 0},
        ))

    sections.append(payload_lib.list_section("cannot-see", "What this report cannot see", footer_notes))
    return header, sections


def _regret_section(optimal, regret_rows):
    """`## Optimal-lineup regret`: a headline, the standing caveat that the
    per-slot gaps do not add, then the per-slot table."""
    if optimal is None:
        headline = payload_lib.insufficient_section(
            "regret-headline", None,
            "at least one rostered player's points read NaN this week; the optimal-lineup "
            "headline is suppressed rather than computed against a missing score",
            level=None,
        )
        data = {"left_on_table": None, "optimal_points": None, "actual_points": None}
    else:
        headline = payload_lib.prose_section(
            "regret-headline", None,
            [f"**{num(optimal['left_on_table'])} points left on the table** "
             f"(optimal {num(optimal['optimal_points'])} vs actual {num(optimal['actual_points'])})."],
            emphasis=True, level=None,
        )
        data = {
            "left_on_table": optimal["left_on_table"],
            "optimal_points": optimal["optimal_points"],
            "actual_points": optimal["actual_points"],
        }

    caveat = payload_lib.prose_section(
        "regret-caveat", None,
        ["_Per-slot gaps below are independent counterfactuals and do not add; for the one number "
         "that does, see \"points left on the table\" above._"],
        level=None,
    )
    if regret_rows:
        columns = [
            payload_lib.column("slot", "slot", "string"),
            payload_lib.column("starter_name", "started", "string"),
            payload_lib.column("starter_points", "starter pts", "number"),
            payload_lib.column("bench_name", "best bench", "string"),
            payload_lib.column("bench_points", "bench pts", "number"),
            payload_lib.column("gap", "gap", "number"),
        ]
        table_block = payload_lib.table_section(
            "regret-rows", None, columns, payload_lib.rows(regret_rows, columns), level=None,
        )
    else:
        table_block = payload_lib.prose_section(
            "regret-rows", None,
            ["No starter was outscored by an eligible bench player this week."], level=None,
        )

    return payload_lib.blocks_section(
        "optimal-lineup-regret", "Optimal-lineup regret",
        [headline, caveat, table_block], data=data,
    )


def build(season, week, team_id=None):
    """Assemble the full Tuesday report as markdown text. `--week` keeps
    Monday's contract exactly -- the current scoring period -- and this
    module reviews `week - 1` internally."""
    team_id = team_id if team_id is not None else config.TEAM_ID
    review_week = week - 1

    if review_week < 1:
        rendered_at = time.time()
        title = f"Week {review_week} in review -- {season}"
        covers = "nothing -- there is no prior week to review"
        lines = header_lines(title, review_week, covers, None, rendered_at)
        lines += [
            "", "No completed week yet -- there is no prior week to review.", "",
            "## What this report cannot see",
        ]
        lines.extend(f"- {n}" for n in FOOTER_NOTES)
        # The one branch whose body precedes any heading, so the lead line is
        # a headingless block rather than a section of its own.
        return payload_lib.RenderedReport("\n".join(lines) + "\n", {
            "header": payload_lib.header_block(title, review_week, covers, None, rendered_at),
            "sections": [
                payload_lib.prose_section(
                    "no-completed-week", None,
                    ["No completed week yet -- there is no prior week to review."], level=None,
                ),
                payload_lib.list_section("cannot-see", "What this report cannot see", FOOTER_NOTES),
            ],
        })

    matchups_df = latest_export("matchups")
    closure = week_closed(matchups_df, review_week, team_id)

    teams_df = latest_export("teams")
    standings_result = standings(teams_df, review_week)

    rosters_df = latest_export("weekly-rosters")
    pool_df = latest_export("player-pool")
    roster_slots_df = latest_export("roster-slots")
    allowed_slots = monday.league_slots(roster_slots_df)

    regret_rows, fallback_names, nan_names = regret_table(rosters_df, review_week, team_id, pool_df, allowed_slots)
    optimal = optimal_lineup(rosters_df, review_week, team_id, pool_df, roster_slots_df, allowed_slots)
    if optimal is not None:
        fallback_names |= optimal["fallback_names"]
    drops = drop_candidates(rosters_df, pool_df, review_week, team_id)
    ir_now, ir_maybe = ir_eligible(rosters_df, review_week, team_id)

    footer_notes = list(FOOTER_NOTES)
    for name, (_, stale) in freshness(season=season).items():
        if stale:
            footer_notes.append(f"The {name} feed is stale as of this report's generation.")
    # Per-view, not the feed-level `freshness()` loop above: that one reads
    # max(fetched_at) across every cached ESPN view, so a fresh player-pool
    # fetch masks a day-old roster. See loaders.roster_read_is_current.
    roster_note = roster_staleness_note(rendered_at, season=season, week=week)
    if roster_note:
        footer_notes.append(roster_note)
    if fallback_names:
        footer_notes.append(
            "Slot eligibility came from the position fallback (no player-pool row this week) for: "
            + ", ".join(sorted(fallback_names))
        )
    if nan_names:
        footer_notes.append(
            "Points read NaN -- excluded from regret and the optimal-lineup headline -- for: "
            + ", ".join(sorted(nan_names))
        )
    if closure.get("state") == "partial":
        footer_notes.append(
            "The league's closure is partial this week; standings and regret may reflect an incomplete week."
        )
    divergence_df = slot_map_divergence(pool_df, allowed_slots)
    if not divergence_df.empty:
        footer_notes.append(
            f"{len(divergence_df)} player(s) in this week's pool have eligible_slots that diverge from "
            "the position-fallback map -- the fallback path may be wrong for them specifically."
        )

    args = (
        season, review_week, closure, standings_result, regret_rows, optimal, drops,
        ir_now, ir_maybe, footer_notes,
    )
    # Pinned once: two emitters each defaulting to their own time.time() would
    # stamp the markdown and the JSON embedding it with different clock reads.
    kwargs = {"window": weeks.week_window(season, review_week), "rendered_at": time.time()}
    header, sections = payload(*args, **kwargs)
    return payload_lib.RenderedReport(
        render(*args, **kwargs), {"header": header, "sections": sections}
    )
