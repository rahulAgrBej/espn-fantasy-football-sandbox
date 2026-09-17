"""Friday -- the lineup lock. The week's most consequential report: it
carries the binding decision to lock the Sunday lineup, held open only for
slots this report deliberately names.

Two upstream feeds land Friday morning that nothing has read until now:
`sleeper-daily` (08:11 ET) completes the Wed/Thu/Fri practice trajectory,
and `odds-line-movement` (10:08 ET) is the second capture of `team_totals`
this week, diffable against Tuesday's `slate` open. See
docs/report-weekly-schedule.md's "Friday -- lineup lock" section for the
full spec this module implements.

Reuses aggressively, like thursday.py: `wednesday.practice_signals`,
`wednesday.is_at_risk`, `availability.read`, `waivers.week_projection`,
`tuesday._eligible_slots`/`_slot_instances`/`_bench`/`solve_slots`,
`monday.league_slots`, and all of `render.py`/`loaders.py`.
"""

import time

import pandas as pd

from .. import config, weeks
from ..odds import projections as odds_projections
from ..odds import store as odds_store
from . import availability, loaders, monday, payload as payload_lib, pool, tuesday, waivers, wednesday
from .loaders import espn_export_warning, freshness, latest_export
from .render import INSUFFICIENT_DATA, freshness_lines, header_lines, num, table

# Chosen, not fitted -- see FOOTER_NOTES. No report in this repo is ever
# scored against what happened (docs/report-weekly-schedule.md's known
# gaps), so these are stated choices rather than backtested values.
SWAP_GAP_POINTS = 3.0
HOLD_OPEN_GAP = 2.0

# The odds job whose last_run gates the movement section -- keyed apart
# from every other job's staleness, same as thursday._PROPS_JOB and
# waivers.team_totals's slate_context precedent.
_MOVEMENT_JOB = "line_movement"

# sleeper_signals.format_trajectory joins three per-day values with " / ",
# an em dash per missing day (Observed: format_trajectory((None, None,
# None)) == "— / — / —"). A player with no Sleeper match at all instead
# carries practice_trajectory = None; callers render both forms the same
# way, since both mean "no signal on any of the three days".
_EMPTY_TRAJECTORY = "— / — / —"

# Tier strings that fire the "tier downgrade" swap rule -- narrower than
# wednesday.is_at_risk's set (which also folds in COIN_FLIP/QUESTIONABLE/
# DOUBTFUL for the broader watchlist purpose). The spec names OUT and
# HIGH_RISK only for this rule.
_TIER_DOWNGRADE = {"OUT", "HIGH_RISK"}


def _normalize_tier(tier):
    if tier is None or (isinstance(tier, float) and pd.isna(tier)):
        return None
    return str(tier).strip().upper().replace(" ", "_")


def _trajectory_is_empty(trajectory):
    return not trajectory or trajectory == _EMPTY_TRAJECTORY


def _fmt_captured_at(value):
    return pd.Timestamp(value).tz_convert(weeks.ET).strftime("%a %Y-%m-%d %H:%M ET")


def line_movement_read(week):
    """{"insufficient", "reason", "rows", "open_at", "current_at"} -- the
    same gate shape as thursday.props_read and waivers.team_totals.

    A reason-string ladder, each rung its own message: no team_totals
    parquet on disk; no line_movement entry in last_run.json (the current
    real state -- data/odds/last_run.json holds only slate_context until
    the first Friday odds job runs); that entry's own stale flag set (the
    budget-abort case docs/odds-budget.md describes); no rows for `week`;
    fewer than two distinct captures for `week` -- an open exists but
    nothing to diff it against, named by its capture time rather than
    rendered as a zero delta.

    On success, `open_at`/`current_at` are the earliest/latest `captured_at`
    for `week` -- **by timestamp, never by run order**, so this stays
    correct if `line_movement` ever lands twice or out of order."""
    empty = {"insufficient": True, "rows": [], "open_at": None, "current_at": None}
    if not config.ODDS_TEAM_TOTALS.exists():
        return {**empty, "reason": "no team_totals.parquet on disk"}

    last_run = odds_store.read_last_run(_MOVEMENT_JOB)
    if last_run is None:
        return {**empty, "reason": f"no {_MOVEMENT_JOB} entry in last_run.json"}
    if last_run.get("stale"):
        return {**empty, "reason": f"{_MOVEMENT_JOB}'s last run is marked stale: {last_run.get('reason')}"}

    totals_df = odds_projections.team_totals_by_capture(week=week)
    if totals_df.empty:
        return {**empty, "reason": f"no team_totals rows for week {week}"}

    captures = sorted(totals_df["captured_at"].unique())
    if len(captures) < 2:
        return {
            **empty,
            "reason": (
                f"only one team_totals capture exists for week {week} "
                f"({_fmt_captured_at(captures[0])}) -- no second capture to diff against"
            ),
            "open_at": captures[0], "current_at": captures[0],
        }

    open_at, current_at = captures[0], captures[-1]
    open_df = totals_df[totals_df["captured_at"] == open_at]
    current_df = totals_df[totals_df["captured_at"] == current_at]
    merged = open_df.merge(current_df, on="team", how="outer", suffixes=("_open", "_current"))

    rows = []
    for _, r in merged.iterrows():
        if pd.isna(r.get("spread_open")) or pd.isna(r.get("spread_current")):
            continue
        rows.append({
            "team": r["team"],
            "spread_open": r["spread_open"], "spread_current": r["spread_current"],
            "spread_delta": r["spread_current"] - r["spread_open"],
            "total_open": r["total_open"], "total_current": r["total_current"],
            "total_delta": r["total_current"] - r["total_open"],
            "implied_open": r["implied_team_total_open"], "implied_current": r["implied_team_total_current"],
            "implied_delta": r["implied_team_total_current"] - r["implied_team_total_open"],
        })
    rows.sort(key=lambda r: r["implied_delta"])

    return {"insufficient": False, "reason": None, "rows": rows, "open_at": open_at, "current_at": current_at}


def recommended_lineup(week_rosters, pool_df, roster_slots_df, allowed_slots):
    """Solve the best legal Sunday lineup from `waivers.week_projection`
    values, pool-first with a roster-row fallback so a bench player and a
    starter are compared on the same column. Drops IR via `tuesday._bench`'s
    existing exclusion and excludes any player with no projection --
    counted and named, never coerced to 0.0.

    Returns {"insufficient", "lineup", "unfilled_slots", "unprojected_names",
    "fallback_names", "candidates", "eligibility", "values"} -- the last
    three are the DP's own working set, kept so held_open_slots can compute
    an independent best-alternative counterfactual without re-deriving
    eligibility."""
    empty = {
        "insufficient": True, "lineup": [], "unfilled_slots": [], "unprojected_names": set(),
        "fallback_names": set(), "candidates": [], "eligibility": [], "values": [],
    }
    if week_rosters.empty or roster_slots_df.empty:
        return {**empty, "reason": "no weekly-rosters or roster-slots export for this week"}

    rosterable = week_rosters[week_rosters["lineup_slot"] != "IR"]
    if rosterable.empty:
        return {**empty, "reason": "no rosterable (non-IR) player found for this week"}

    candidates, eligibility, values = [], [], []
    unprojected_names, fallback_names = set(), set()
    for _, row in rosterable.iterrows():
        value, _source = waivers.week_projection(row["player_id"], pool_df, row)
        if value is None or pd.isna(value):
            unprojected_names.add(row["player_name"])
            continue
        slots, used_fallback = tuesday._eligible_slots(row["player_id"], row["position"], pool_df, allowed_slots)
        if used_fallback:
            fallback_names.add(row["player_name"])
        candidates.append({"player_id": row["player_id"], "player_name": row["player_name"]})
        eligibility.append(slots)
        values.append(value)

    slot_list = tuesday._slot_instances(roster_slots_df)
    total, ordered_slots, chosen = tuesday.solve_slots(values, eligibility, slot_list)

    lineup = [
        {
            "slot": ordered_slots[i], "player_id": candidates[idx]["player_id"],
            "player_name": candidates[idx]["player_name"], "projection": values[idx],
        }
        for i, idx in enumerate(chosen) if idx != -1
    ]
    unfilled_slots = [ordered_slots[i] for i, idx in enumerate(chosen) if idx == -1]

    return {
        "insufficient": False, "reason": None,
        "total_projected": total, "lineup": lineup, "unfilled_slots": unfilled_slots,
        "unprojected_names": unprojected_names, "fallback_names": fallback_names,
        "candidates": candidates, "eligibility": eligibility, "values": values,
    }


def _current_by_slot(starters, pool_df):
    by_slot = {}
    for _, r in starters.iterrows():
        projection, _source = waivers.week_projection(r["player_id"], pool_df, r)
        by_slot.setdefault(r["lineup_slot"], []).append({
            "player_id": r["player_id"], "player_name": r["player_name"],
            "projection": projection, "pro_team": r.get("pro_team"),
        })
    return by_slot


def lineup_diff(starters, recommended_rows, pool_df, avail_by_id, implied_delta_by_team, pro_team_by_id):
    """Per-slot-*name* diff between the current lineup and the
    recommendation -- never by slot instance, since `solve_slots` assigns
    to instances in most-constrained-first order, which has no relation to
    how the current lineup lists its own starters. Two `RB` instances
    holding the same two players in the opposite order must emit zero rows;
    grouping by name and taking `recommended - current` / `current -
    recommended` per name is the only comparison that gets this right.

    Each changed row names every rule that fired: `projection gap` (the
    incoming player beats the outgoing one by >= SWAP_GAP_POINTS),
    `tier downgrade` (the outgoing starter's tier reads OUT or HIGH_RISK),
    `team total down` (the outgoing starter's team's implied_team_total
    fell since Tuesday's open). More than one rule can fire; all are kept.

    Returns a list of rows, one per occupant of each slot name. An
    unchanged occupant (present in both the current lineup and the
    recommendation) renders as its own row with `changed=False` and empty
    `reasons` -- the pairing that produces the swap rows below is scoped to
    the leftovers *after* every player common to both sides is matched to
    itself, which is what makes two `RB` instances holding the same two
    players in a different internal order emit zero *changed* rows."""
    current_by_slot = _current_by_slot(starters, pool_df)
    recommended_by_slot = {}
    for row in recommended_rows:
        recommended_by_slot.setdefault(row["slot"], []).append(row)

    rows = []
    for slot in sorted(set(current_by_slot) | set(recommended_by_slot)):
        current = current_by_slot.get(slot, [])
        recommended = recommended_by_slot.get(slot, [])
        current_ids = {c["player_id"] for c in current}
        recommended_ids = {r["player_id"] for r in recommended}

        for p in current:
            if p["player_id"] in recommended_ids:
                rows.append({"slot": slot, "current": [p], "recommended": [p], "reasons": [], "changed": False})

        going_out = [c for c in current if c["player_id"] not in recommended_ids]
        coming_in = [r for r in recommended if r["player_id"] not in current_ids]
        going_out.sort(key=lambda c: c["projection"] if c["projection"] is not None else -1.0, reverse=True)
        coming_in.sort(key=lambda r: r["projection"], reverse=True)

        for i in range(max(len(going_out), len(coming_in))):
            out_p = going_out[i] if i < len(going_out) else None
            in_p = coming_in[i] if i < len(coming_in) else None
            reasons = []
            if out_p is not None and in_p is not None:
                if (
                    out_p["projection"] is not None and pd.notna(out_p["projection"])
                    and in_p["projection"] - out_p["projection"] >= SWAP_GAP_POINTS
                ):
                    reasons.append("projection gap")
                if _normalize_tier(avail_by_id.get(out_p["player_id"])) in _TIER_DOWNGRADE:
                    reasons.append("tier downgrade")
                team = out_p.get("pro_team") or pro_team_by_id.get(out_p["player_id"])
                delta = implied_delta_by_team.get(team)
                if delta is not None and delta < 0:
                    reasons.append("team total down")
            rows.append({"slot": slot, "current": [out_p] if out_p else [], "recommended": [in_p] if in_p else [],
                         "reasons": reasons, "changed": True})
    return rows


def _best_alternative(slot, exclude_player_id, candidates, eligibility, values):
    """The highest-projected candidate eligible for `slot`, other than
    `exclude_player_id` -- an independent counterfactual, the same
    no-assignment-constraint convention tuesday.regret_table uses, not a
    re-solve of the rest of the lineup."""
    best = None
    for i, c in enumerate(candidates):
        if c["player_id"] == exclude_player_id or slot not in eligibility[i]:
            continue
        if best is None or values[i] > best["projection"]:
            best = {"player_id": c["player_id"], "player_name": c["player_name"], "projection": values[i]}
    return best


def held_open_slots(recommended, avail_by_id, trajectory_by_id):
    """Slots deliberately left for Sunday's `pre_lock` read, per the spec:
    "holding a slot open should be a stated choice ... so Sunday knows what
    it owes an answer on."

    Held when **both** the recommended starter is at-risk
    (`wednesday.is_at_risk`, which normalizes all three availability
    vocabularies) and the gap to the best legal alternative is below
    `HOLD_OPEN_GAP`. A clearly-better alternative swaps now instead of
    waiting; a healthy starter has nothing new coming Sunday to wait for.

    Plus one unconditional hold: a recommended starter whose trajectory
    reads `-- / -- / --` and whose tier is unresolved -- Friday's own
    standing gap, better deferred explicitly than locked on absent signal.
    """
    held = []
    candidates, eligibility, values = recommended["candidates"], recommended["eligibility"], recommended["values"]
    for row in recommended["lineup"]:
        slot, player_id, player_name, projection = row["slot"], row["player_id"], row["player_name"], row["projection"]
        tier = avail_by_id.get(player_id)
        trajectory = trajectory_by_id.get(player_id)
        alternative = _best_alternative(slot, player_id, candidates, eligibility, values)
        gap = (projection - alternative["projection"]) if alternative else None

        if wednesday.is_at_risk(tier) and alternative is not None and gap is not None and gap < HOLD_OPEN_GAP:
            held.append({
                "slot": slot, "player_name": player_name,
                "reason": (
                    f"{player_name} is {tier}; best legal alternative {alternative['player_name']} is only "
                    f"{gap:.1f} back -- close enough for Sunday's pre_lock read to flip"
                ),
            })
        elif _trajectory_is_empty(trajectory) and tier in (None, INSUFFICIENT_DATA):
            held.append({
                "slot": slot, "player_name": player_name,
                "reason": (
                    f"{player_name}'s practice trajectory is {_EMPTY_TRAJECTORY} and tier is unresolved -- "
                    "deferring on absent signal rather than locking against it"
                ),
            })
    return held


def ranked_bench(bench, pool_df):
    """The full bench, ranked by `waivers.week_projection` regardless of
    whether any swap fired above -- so the flex decision is visible rather
    than asserted. NaN projections sort last, named rather than dropped."""
    rows = []
    for _, r in bench.iterrows():
        value, source = waivers.week_projection(r["player_id"], pool_df, r)
        rows.append({"player_name": r["player_name"], "position": r["position"], "projection": value, "source": source})
    rows.sort(key=lambda r: (r["projection"] is None or pd.isna(r["projection"]),
                             -(r["projection"] if r["projection"] is not None and not pd.isna(r["projection"]) else 0.0)))
    return rows


def streaming_drop_candidates(rosters_df, pool_df, week, team_id, free_agents_df):
    """Drop candidates (`tuesday.drop_candidates`) narrowed to only those
    that would make room for a weekend D/ST or K streaming add -- per the
    spec, drop candidates on this report are scoped to that one purpose,
    not the general bench-weakness list Tuesday's waiver report already
    owns. Returns (rows, reason_if_none) where each row pairs a legal drop
    with the streaming candidate(s) it would make room for."""
    drops = tuesday.drop_candidates(rosters_df, pool_df, week, team_id)
    if not drops:
        return [], "no legal drop candidate exists this week"

    week_rosters = rosters_df[(rosters_df["week"] == week) & (rosters_df["team_id"] == team_id)]
    starters = week_rosters[week_rosters["started"]] if not week_rosters.empty else week_rosters

    streams = []
    for position in ("D/ST", "K"):
        current = starters[starters["position"] == position] if not starters.empty else starters
        current_proj = None
        if not current.empty:
            current_proj, _ = waivers.week_projection(current.iloc[0]["player_id"], pool_df, current.iloc[0])
        candidates = (
            free_agents_df[free_agents_df["position"] == position] if not free_agents_df.empty else pd.DataFrame()
        )
        best = None
        for fa in candidates.itertuples():
            value, _ = waivers.week_projection(fa.player_id, pool_df)
            if value is None or pd.isna(value):
                continue
            if current_proj is not None and not pd.isna(current_proj) and value <= current_proj:
                continue
            if best is None or value > best["week_projected"]:
                best = {"player_name": fa.player_name, "position": position, "week_projected": value}
        if best:
            streams.append(best)

    if not streams:
        return [], "no free-agent D/ST or K projects above our current starter"

    return [{"drop": d, "streams": streams} for d in drops], None


FOOTER_NOTES = [
    "A `-- / -- / --` trajectory means a Sleeper collection day was missed. That is permanent, "
    "not deferred -- Sleeper publishes no history to backfill from -- and Friday is the report "
    "most degraded by a missed day, so it says so explicitly rather than falling back to "
    "injury_status alone.",
    "A sharp line move since Tuesday is the market's own read on the same injury news the rest "
    "of this report is built from -- worth cross-checking against, never deferring to.",
    "SWAP_GAP_POINTS and HOLD_OPEN_GAP are chosen, not fitted -- no report in this repo is ever "
    "scored against what happened.",
    "Official inactives drop roughly 90 minutes before Sunday kickoff and appear in no feed this "
    "pipeline touches.",
    "This filename carries the render date, not the covered one, same convention as every other report.",
]


def render(season, week, team_id, movement, recommended, diff_rows, held_rows, bench_rows,
           practice_df, streaming_drops, streaming_drop_reason, footer_notes, window=None, rendered_at=None):
    """Pure over its arguments -- no disk access beyond the freshness
    header, same contract as thursday.render/wednesday.render."""
    rendered_at = rendered_at if rendered_at is not None else time.time()
    title = f"Lineup lock -- {season} week {week}"
    covers = f"week {week}'s Sunday lineup lock"

    lines = header_lines(title, week, covers, window, rendered_at)
    lines.append("")

    lines.append("## Freshness")
    lines.extend(freshness_lines(freshness(season=season)))
    lines.append("")

    lines.append("## Decisions due")
    lines.append("**Lock the Sunday lineup**, except for any slot deliberately held open below.")
    if recommended["insufficient"]:
        lines.append(f"**{INSUFFICIENT_DATA}** -- {recommended['reason']}")
    elif held_rows:
        lines.append(f"{len(held_rows)} slot(s) held open for Sunday's `pre_lock` read:")
        for h in held_rows:
            lines.append(f"- **{h['slot']}** ({h['player_name']}) -- {h['reason']}")
    else:
        lines.append("No slot is held open -- every recommended starter is either healthy or has no close legal alternative.")
    lines.append("")

    lines.append("## Recommended lineup")
    if recommended["insufficient"]:
        lines.append(f"**{INSUFFICIENT_DATA}** -- {recommended['reason']}")
    else:
        lines.append(
            f"**{num(recommended['total_projected'])} total projected points** across the solved lineup."
        )
        lines.append("")
        headers = ["slot", "current", "proj", "recommended", "proj", "gap", "rule(s) fired", "status"]
        rows = []
        held_names = {h["player_name"] for h in held_rows}
        for d in diff_rows:
            current_names = ", ".join(c["player_name"] for c in d["current"]) or "(none)"
            current_proj = d["current"][0]["projection"] if d["current"] else None
            recommended_names = ", ".join(r["player_name"] for r in d["recommended"]) or "(none)"
            recommended_proj = d["recommended"][0]["projection"] if d["recommended"] else None
            gap = (
                recommended_proj - current_proj
                if recommended_proj is not None and current_proj is not None and pd.notna(current_proj)
                else None
            )
            touches_held = (d["current"] and d["current"][0]["player_name"] in held_names) or \
                (d["recommended"] and d["recommended"][0]["player_name"] in held_names)
            if touches_held:
                status = "held open"
            elif not d["changed"]:
                status = "unchanged"
            elif d["reasons"]:
                status = "swap"
            else:
                status = "hold (below threshold)"
            rows.append([
                d["slot"], current_names, num(current_proj), recommended_names, num(recommended_proj),
                num(gap), ", ".join(d["reasons"]) or "--", status,
            ])
        lines.extend(table(headers, rows))
        if recommended["unfilled_slots"]:
            lines.append("")
            lines.append(
                f"_{len(recommended['unfilled_slots'])} slot(s) had no projected, eligible candidate: "
                + ", ".join(recommended["unfilled_slots"]) + "._"
            )
    lines.append("")

    lines.append("## Bench")
    if not bench_rows:
        lines.append(f"**{INSUFFICIENT_DATA}** -- no bench could be read for week {week}.")
    else:
        headers = ["player", "position", "week_projected", "source"]
        rows = [[r["player_name"], r["position"], num(r["projection"]), r["source"] or "--"] for r in bench_rows]
        lines.extend(table(headers, rows))
    lines.append("")

    lines.append("## Line movement since Tuesday's open")
    if movement["insufficient"]:
        lines.append(f"**{INSUFFICIENT_DATA}** -- {movement['reason']}")
    else:
        lines.append(
            f"Open {_fmt_captured_at(movement['open_at'])} -> current {_fmt_captured_at(movement['current_at'])}."
        )
        headers = ["team", "spread open", "spread now", "d(spread)", "total open", "total now", "d(total)",
                   "implied open", "implied now", "d(implied)"]
        rows = [
            [r["team"], num(r["spread_open"]), num(r["spread_current"]), num(r["spread_delta"]),
             num(r["total_open"]), num(r["total_current"]), num(r["total_delta"]),
             num(r["implied_open"]), num(r["implied_current"]), num(r["implied_delta"])]
            for r in movement["rows"]
        ]
        lines.extend(table(headers, rows))
    lines.append("")

    lines.append("## Practice report -- Wed / Thu / Fri")
    if practice_df.empty:
        lines.append(f"**{INSUFFICIENT_DATA}** -- no roster or no Sleeper snapshot to read.")
    else:
        headers = ["player", "trajectory (W/T/F)", "tier"]
        rows = [
            [r.get("player_name"), r.get("practice_trajectory") or _EMPTY_TRAJECTORY, r.get("tier") or INSUFFICIENT_DATA]
            for _, r in practice_df.iterrows()
        ]
        lines.extend(table(headers, rows))
    lines.append("")

    lines.append("## Drop candidates")
    if streaming_drop_reason:
        lines.append(f"_None -- {streaming_drop_reason}._")
    else:
        for entry in streaming_drops:
            stream_names = ", ".join(f"{s['player_name']} ({num(s['week_projected'])} proj)" for s in entry["streams"])
            lines.append(f"- Drop {entry['drop']['player_name']} ({entry['drop']['position']}) for: {stream_names}")
    lines.append("")

    lines.append("## What this report cannot see")
    lines.extend(f"- {n}" for n in footer_notes)
    return "\n".join(lines) + "\n"


def payload(season, week, team_id, movement, recommended, diff_rows, held_rows, bench_rows,
            practice_df, streaming_drops, streaming_drop_reason, footer_notes,
            window=None, rendered_at=None):
    """The structured twin of `render`, over the identical argument list. See
    espn_ff/report/payload.py."""
    rendered_at = rendered_at if rendered_at is not None else time.time()
    title = f"Lineup lock -- {season} week {week}"
    covers = f"week {week}'s Sunday lineup lock"

    header = payload_lib.header_block(title, week, covers, window, rendered_at)
    sections = [
        payload_lib.freshness_section(freshness(season=season)),
        _decisions_section(recommended, held_rows),
        _recommended_section(recommended, diff_rows, held_rows),
    ]

    if not bench_rows:
        sections.append(payload_lib.insufficient_section(
            "bench", "Bench", f"no bench could be read for week {week}",
        ))
    else:
        columns = [
            payload_lib.column("player_name", "player", "string"),
            payload_lib.column("position", "position", "string"),
            payload_lib.column("projection", "week_projected", "number"),
            payload_lib.column("source", "source", "string"),
        ]
        sections.append(payload_lib.table_section(
            "bench", "Bench", columns, payload_lib.rows(bench_rows, columns),
        ))

    sections.append(_movement_section(movement))

    if practice_df.empty:
        sections.append(payload_lib.insufficient_section(
            "practice-report", "Practice report -- Wed / Thu / Fri",
            "no roster or no Sleeper snapshot to read",
        ))
    else:
        columns = [
            payload_lib.column("player_name", "player", "string"),
            payload_lib.column("practice_trajectory", "trajectory (W/T/F)", "string"),
            payload_lib.column("tier", "tier", "string"),
        ]
        sections.append(payload_lib.table_section(
            "practice-report", "Practice report -- Wed / Thu / Fri", columns,
            payload_lib.rows(practice_df, columns),
        ))

    sections.append(_streaming_drops_section(streaming_drops, streaming_drop_reason))
    sections.append(payload_lib.list_section("cannot-see", "What this report cannot see", footer_notes))
    return header, sections


def _decisions_section(recommended, held_rows):
    body = ["**Lock the Sunday lineup**, except for any slot deliberately held open below."]
    if recommended["insufficient"]:
        body.append(f"**{INSUFFICIENT_DATA}** -- {recommended['reason']}")
    elif held_rows:
        body.append(f"{len(held_rows)} slot(s) held open for Sunday's `pre_lock` read:")
        body.extend(f"- **{h['slot']}** ({h['player_name']}) -- {h['reason']}" for h in held_rows)
    else:
        body.append("No slot is held open -- every recommended starter is either healthy or has "
                    "no close legal alternative.")
    return payload_lib.prose_section(
        "decisions-due", "Decisions due", body, emphasis=True,
        # The held-open slots are Sunday's actual worklist, and in markdown
        # they exist only as bullet prose.
        data={
            "binding": True,
            "held_open": [
                {"slot": h["slot"], "player_name": h["player_name"], "reason": h["reason"]}
                for h in held_rows
            ] if not recommended["insufficient"] else None,
        },
    )


def _recommended_section(recommended, diff_rows, held_rows):
    if recommended["insufficient"]:
        return payload_lib.insufficient_section(
            "recommended-lineup", "Recommended lineup", recommended["reason"],
        )

    columns = [
        payload_lib.column("slot", "slot", "string"),
        payload_lib.column("current", "current", "string"),
        payload_lib.column("current_projection", "current proj", "number"),
        payload_lib.column("recommended", "recommended", "string"),
        payload_lib.column("recommended_projection", "recommended proj", "number"),
        payload_lib.column("gap", "gap", "number"),
        payload_lib.column("reasons", "rule(s) fired", "string"),
        payload_lib.column("status", "status", "string"),
        payload_lib.column("changed", "changed", "boolean"),
    ]
    held_names = {h["player_name"] for h in held_rows}
    rows = []
    for d in diff_rows:
        current_proj = d["current"][0]["projection"] if d["current"] else None
        recommended_proj = d["recommended"][0]["projection"] if d["recommended"] else None
        gap = (
            recommended_proj - current_proj
            if recommended_proj is not None and current_proj is not None and pd.notna(current_proj)
            else None
        )
        touches_held = (d["current"] and d["current"][0]["player_name"] in held_names) or \
            (d["recommended"] and d["recommended"][0]["player_name"] in held_names)
        if touches_held:
            status = "held open"
        elif not d["changed"]:
            status = "unchanged"
        elif d["reasons"]:
            status = "swap"
        else:
            status = "hold (below threshold)"
        rows.append({
            "slot": payload_lib.unset(d["slot"]),
            # Joined, as the markdown joins them: a slot can hold more than
            # one player, and the names are the cell's content either way.
            "current": ", ".join(c["player_name"] for c in d["current"]) or None,
            "current_projection": payload_lib.unset(current_proj),
            "recommended": ", ".join(r["player_name"] for r in d["recommended"]) or None,
            "recommended_projection": payload_lib.unset(recommended_proj),
            "gap": payload_lib.unset(gap),
            # A list, not the markdown's comma-joined string: these are
            # discrete rule names and a consumer may want to filter on one.
            "reasons": list(d["reasons"]),
            "status": status,
            "changed": bool(d["changed"]),
        })

    notes = []
    if recommended["unfilled_slots"]:
        notes.append(
            f"_{len(recommended['unfilled_slots'])} slot(s) had no projected, eligible candidate: "
            + ", ".join(recommended["unfilled_slots"]) + "._"
        )
    return payload_lib.table_section(
        "recommended-lineup", "Recommended lineup", columns, rows, notes=notes,
        data={
            "total_projected": recommended["total_projected"],
            "unfilled_slots": recommended["unfilled_slots"],
        },
    )


def _movement_section(movement):
    heading = "Line movement since Tuesday's open"
    if movement["insufficient"]:
        return payload_lib.insufficient_section("line-movement", heading, movement["reason"])
    columns = [
        payload_lib.column("team", "team", "string"),
        payload_lib.column("spread_open", "spread open", "number"),
        payload_lib.column("spread_current", "spread now", "number"),
        payload_lib.column("spread_delta", "d(spread)", "number"),
        payload_lib.column("total_open", "total open", "number"),
        payload_lib.column("total_current", "total now", "number"),
        payload_lib.column("total_delta", "d(total)", "number"),
        payload_lib.column("implied_open", "implied open", "number"),
        payload_lib.column("implied_current", "implied now", "number"),
        payload_lib.column("implied_delta", "d(implied)", "number"),
    ]
    return payload_lib.table_section(
        "line-movement", heading, columns, payload_lib.rows(movement["rows"], columns),
        notes=[f"Open {_fmt_captured_at(movement['open_at'])} -> current "
               f"{_fmt_captured_at(movement['current_at'])}."],
        data={"open_at": movement["open_at"], "current_at": movement["current_at"]},
    )


def _streaming_drops_section(streaming_drops, streaming_drop_reason):
    """`## Drop candidates` -- each entry pairs one droppable player with the
    streaming adds that drop would fund, so the pairing is preserved as
    nested rows rather than flattened into one sentence per line."""
    if streaming_drop_reason:
        return payload_lib.prose_section(
            "drop-candidates", "Drop candidates", [f"_None -- {streaming_drop_reason}._"],
            data={"count": 0, "reason": streaming_drop_reason},
        )
    columns = [
        payload_lib.column("player_name", "drop", "string"),
        payload_lib.column("position", "position", "string"),
        payload_lib.column("streams", "for", "string"),
    ]
    rows = [
        {
            "player_name": payload_lib.unset(entry["drop"]["player_name"]),
            "position": payload_lib.unset(entry["drop"]["position"]),
            "streams": [
                {"player_name": payload_lib.unset(s["player_name"]),
                 "week_projected": payload_lib.unset(s["week_projected"])}
                for s in entry["streams"]
            ],
        }
        for entry in streaming_drops
    ]
    return payload_lib.table_section(
        "drop-candidates", "Drop candidates", columns, rows,
        data={"count": len(streaming_drops), "reason": None},
    )


def build(season, week, team_id=None):
    """Assemble the full Friday report as markdown text. `week` is the
    current scoring period with no offset -- Friday looks forward at the
    week in progress, same contract as wednesday.build."""
    team_id = team_id if team_id is not None else config.TEAM_ID
    rendered_at = time.time()

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
    pro_team_by_id = (
        dict(zip(week_rosters["player_id"], week_rosters["pro_team"])) if not week_rosters.empty else {}
    )

    today = pd.Timestamp(rendered_at, unit="s", tz="UTC").tz_convert(weeks.ET).date()
    signals_df = wednesday.practice_signals(week_rosters, today=today) if not week_rosters.empty else pd.DataFrame()
    trajectory_by_id = (
        dict(zip(signals_df["player_id"], signals_df["practice_trajectory"])) if not signals_df.empty else {}
    )
    practice_df = pd.DataFrame()
    if not signals_df.empty and not week_rosters.empty:
        practice_df = signals_df.merge(week_rosters[["player_id", "player_name"]], on="player_id", how="left")
        practice_df["tier"] = practice_df["player_id"].map(avail_by_id)

    recommended = recommended_lineup(week_rosters, pool_df, roster_slots_df, allowed_slots)

    movement = line_movement_read(week)
    implied_delta_by_team = (
        {r["team"]: r["implied_delta"] for r in movement["rows"]} if not movement["insufficient"] else {}
    )

    diff_rows, held_rows, bench_rows = [], [], []
    if not recommended["insufficient"]:
        if not starters.empty:
            diff_rows = lineup_diff(starters, recommended["lineup"], pool_df, avail_by_id, implied_delta_by_team, pro_team_by_id)
        held_rows = held_open_slots(recommended, avail_by_id, trajectory_by_id)
    if not bench.empty:
        bench_rows = ranked_bench(bench, pool_df)

    free_agents_df = pool.free_agents(week, pool_df=pool_df, rosters_df=rosters_df)
    streaming_drops, streaming_drop_reason = streaming_drop_candidates(
        rosters_df, pool_df, week, team_id, free_agents_df
    )

    footer_notes = list(FOOTER_NOTES)
    for name, (_, stale) in freshness(season=season).items():
        if stale:
            footer_notes.append(f"The {name} feed is stale as of this report's generation.")
    export_warning = espn_export_warning()
    if export_warning:
        footer_notes.append(f"The last ESPN export shrank -- {export_warning}.")
    if movement["insufficient"]:
        footer_notes.append(f"Line movement could not be read -- {movement['reason']}.")
    if not recommended["insufficient"]:
        if recommended["unprojected_names"]:
            footer_notes.append(
                "Excluded from the lineup solve for want of a projection (never coerced to 0.0): "
                + ", ".join(sorted(recommended["unprojected_names"]))
            )
        if recommended["fallback_names"]:
            footer_notes.append(
                "Slot eligibility came from the position fallback (no player-pool row) for: "
                + ", ".join(sorted(recommended["fallback_names"]))
            )
    else:
        footer_notes.append(f"The lineup solve could not run -- {recommended['reason']}.")
    if not signals_df.empty and "matched" in signals_df:
        unmatched = signals_df[~signals_df["matched"]]
        if not unmatched.empty and not week_rosters.empty:
            names_by_id = dict(zip(week_rosters["player_id"], week_rosters["player_name"]))
            names = sorted(names_by_id.get(pid, str(pid)) for pid in unmatched["player_id"])
            footer_notes.append(
                "No Sleeper id match -- practice trajectory is blank, not clear -- for: " + ", ".join(names)
            )
    if held_rows:
        footer_notes.append(f"{len(held_rows)} slot(s) held open -- see Decisions due.")
    divergence_df = tuesday.slot_map_divergence(pool_df, allowed_slots)
    if not divergence_df.empty:
        footer_notes.append(
            f"{len(divergence_df)} player(s) in this week's pool have eligible_slots that diverge from "
            "the position-fallback map -- the fallback path may be wrong for them specifically."
        )

    args = (
        season, week, team_id, movement, recommended, diff_rows, held_rows, bench_rows,
        practice_df, streaming_drops, streaming_drop_reason, footer_notes,
    )
    kwargs = {"window": weeks.week_window(season, week), "rendered_at": rendered_at}
    header, sections = payload(*args, **kwargs)
    return payload_lib.RenderedReport(
        render(*args, **kwargs), {"header": header, "sections": sections}
    )
