"""Thursday -- usage and market.

The week's only weekday carrying a hard binding decision: the
Thursday-night start/sit, binding at kickoff. It is also the first day
both of its inputs are actually worth reading -- `nflverse-thursday`
forces a full re-download at 09:53 ET (the first pull where `provisional`
resolves false for last week's games), and `odds-props` captures player
props at 10:08 ET. See docs/report-weekly-schedule.md's "Thursday -- usage
and market" section for the full spec this module implements.

Two week numbers, deliberately different: `usage_week = week - 1` (the
canonical read of last week, like tuesday.build's `review_week`), while
props, practice trajectory and the Thursday-night game are all **this**
week. Conflating them is the error this report is most likely to make, so
the dateline names both explicitly.

Reuses aggressively -- this module defines very little of its own.
Cross-day imports are the repo's convention (wednesday.py imports monday,
tuesday, waivers; this module imports all three plus wednesday itself).
"""

import time

import pandas as pd

from .. import config, weeks
from ..odds import projections as odds_projections
from ..odds import store as odds_store
from . import availability, loaders, monday, payload as payload_lib, schedule, tuesday, waivers, wednesday
from .loaders import espn_export_warning, freshness, latest_export, roster_staleness_note
from .render import INSUFFICIENT_DATA, freshness_lines, header_lines, num, table

# Chosen, not fitted -- see FOOTER_NOTES and the dynamic note build() adds.
PROJECTION_DIVERGENCE_PCT = 0.25

# The odds job whose last_run gates section 5 -- keyed apart from every
# other job's staleness, same as waivers.py's slate_context precedent.
_PROPS_JOB = "props_primary"


def _round_or_none(value, digits=3):
    """None/NaN pass through as None so `render.table`'s own gap-rendering
    fires (`--`), rather than being pre-formatted into a string that table()
    can no longer recognize as missing. `snap_pct_delta_3w` is NaN before
    week 4 and `snap_pct_delta_1w` before week 2 by construction (nflverse
    role-feature window functions) -- this must render the gap, never 0.0."""
    if value is None or pd.isna(value):
        return None
    return round(float(value), digits)


def _int_or_none(value):
    if value is None or pd.isna(value):
        return None
    return int(value)


def canonical_read(features_df, usage_week):
    """{"insufficient", "reason", "provisional"} gate for the canonical-usage
    section, mirroring waivers.waiver_read_is_settled's shape: gate on the
    input's own freshness, never on slot ordering.

    No rows for (season, usage_week) at all -> insufficient, naming
    nflverse's actual last fetch of the datasets `features.build` reads.
    Rows present but `provisional` still true -> NOT insufficient -- the
    table still renders, bannered as not canonical, per the spec's "say so
    rather than presenting them as final"."""
    if features_df.empty:
        fetched_at = loaders.nflverse_features_freshness()
        if fetched_at is None:
            fetched = "nflverse has never been fetched"
        else:
            fetched = f"nflverse last fetched {_fmt_fetched(fetched_at)}"
        return {
            "insufficient": True,
            "reason": f"no player_week_features rows for week {usage_week} ({fetched})",
            "provisional": None,
        }
    return {"insufficient": False, "reason": None, "provisional": bool(features_df["provisional"].any())}


def _fmt_fetched(ts):
    return pd.Timestamp(ts, unit="s", tz="UTC").tz_convert(weeks.ET).strftime("%a %Y-%m-%d %H:%M ET")


def props_read(week):
    """{"insufficient", "reason", "props_df"} gate for the market section,
    mirroring waivers.team_totals exactly: parquet absent -> `props_primary`
    last-run entry absent -> entry stale -> frame empty for the week, each
    with its own reason string."""
    empty = {"insufficient": True, "props_df": pd.DataFrame()}
    if not config.ODDS_PROPS.exists():
        return {**empty, "reason": "no player_props.parquet on disk"}

    last_run = odds_store.read_last_run(_PROPS_JOB)
    if last_run is None:
        return {**empty, "reason": f"no {_PROPS_JOB} entry in last_run.json"}
    if last_run.get("stale"):
        return {**empty, "reason": f"{_PROPS_JOB}'s last run is marked stale: {last_run.get('reason')}"}

    props_points_df, _ = odds_projections.build(week=week)
    if props_points_df.empty:
        return {
            **empty,
            "reason": f"no props rows for week {week} -- the market has not consolidated for this "
                      "slate yet, not that no props exist",
        }
    return {"insufficient": False, "reason": None, "props_df": props_points_df}


def market_points(props_points_df, pool_df, xwalk_df=None, sleeper_map_df=None):
    """Sum `fantasy_points` per `espn_player_id` across markets -- a player
    is quoted on several markets (rush yds, rec yds, anytime TD...) and ESPN
    publishes one projection. `markets` rides along per player: a
    single-market player's total is understated, surfaced in the footer.
    `match_source == "unmatched"` rows are excluded from `by_player` (there
    is no player to attribute points to) but counted, never silently
    dropped from the underlying resolved frame. `xwalk_df`/`sleeper_map_df`
    pass straight through to `resolve_props` -- production omits them (disk
    defaults), tests inject empty frames to isolate from whatever is on
    disk in a real checkout."""
    if props_points_df is None or props_points_df.empty:
        return {"by_player": {}, "unmatched_count": 0}

    resolved, unmatched = odds_projections.resolve_props(
        props_points_df, espn_players_df=pool_df, xwalk_df=xwalk_df, sleeper_map_df=sleeper_map_df
    )
    matched = resolved[resolved["match_source"] != "unmatched"]

    pool_names = dict(zip(pool_df["player_id"], pool_df["player_name"])) if not pool_df.empty else {}
    by_player = {}
    if not matched.empty:
        grouped = matched.groupby("espn_player_id").agg(
            points=("fantasy_points", "sum"), markets=("market", "nunique")
        )
        for pid, row in grouped.iterrows():
            by_player[pid] = {
                "points": row["points"], "markets": int(row["markets"]),
                "player_name": pool_names.get(pid, f"(player {pid})"),
            }
    return {"by_player": by_player, "unmatched_count": len(unmatched)}


def divergence(market_by_player, pool_df):
    """Flags where `abs(prop_total - espn_projected) / espn_projected >=
    PROJECTION_DIVERGENCE_PCT`, `espn_projected` non-null and > 0. A zero or
    NaN ESPN projection is counted separately as "could not compare" rather
    than silently excluded or compared against zero. Returns (rows,
    could_not_compare_count)."""
    if not market_by_player or pool_df.empty:
        return [], len(market_by_player)

    pool_by_id = {r["player_id"]: r for _, r in pool_df.iterrows()}
    rows, could_not_compare = [], 0
    for pid, info in market_by_player.items():
        row = pool_by_id.get(pid)
        espn_projected = row.get("week_projected") if row is not None else None
        prop_total = info.get("points")
        if row is None or espn_projected is None or pd.isna(espn_projected) or espn_projected <= 0 \
                or prop_total is None or pd.isna(prop_total):
            could_not_compare += 1
            continue
        pct = abs(prop_total - espn_projected) / espn_projected
        if pct >= PROJECTION_DIVERGENCE_PCT:
            rows.append({
                "player_name": info["player_name"], "prop_total": prop_total,
                "espn_projected": espn_projected, "pct": pct, "markets": info["markets"],
            })
    rows.sort(key=lambda r: r["pct"], reverse=True)
    return rows, could_not_compare


def swap_candidates(starters, bench, features_by_id, pool_df, allowed_slots):
    """Starters whose `snap_pct_delta_1w` fell (< 0) against a same-slot
    bench player whose delta rose (> 0), ranked by the `week_projected` gap.
    `wopr` rides along on both sides so a target-share story sits next to a
    snap-count one."""
    if starters.empty or bench.empty or not features_by_id:
        return []

    rows = []
    for _, starter in starters.iterrows():
        s_feat = features_by_id.get(starter["player_id"])
        s_delta = s_feat.get("snap_pct_delta_1w") if s_feat is not None else None
        if s_delta is None or pd.isna(s_delta) or s_delta >= 0:
            continue
        target_slot = starter["lineup_slot"]

        best = None
        for _, bp in bench.iterrows():
            b_feat = features_by_id.get(bp["player_id"])
            b_delta = b_feat.get("snap_pct_delta_1w") if b_feat is not None else None
            if b_delta is None or pd.isna(b_delta) or b_delta <= 0:
                continue
            bp_slots, _ = tuesday._eligible_slots(bp["player_id"], bp["position"], pool_df, allowed_slots)
            if target_slot not in bp_slots:
                continue
            value, _ = waivers.week_projection(bp["player_id"], pool_df, bp)
            if value is None or pd.isna(value):
                continue
            if best is None or value > best["week_projected"]:
                best = {
                    "player_name": bp["player_name"], "week_projected": value,
                    "wopr": b_feat.get("wopr"), "snap_pct_delta_1w": b_delta,
                }
        if best is None:
            continue

        s_value, _ = waivers.week_projection(starter["player_id"], pool_df, starter)
        gap = (best["week_projected"] - s_value) if s_value is not None and pd.notna(s_value) else None
        rows.append({
            "starter_name": starter["player_name"], "starter_delta": s_delta,
            "starter_wopr": s_feat.get("wopr"), "starter_projected": s_value,
            "bench_name": best["player_name"], "bench_delta": best["snap_pct_delta_1w"],
            "bench_wopr": best["wopr"], "bench_projected": best["week_projected"], "gap": gap,
        })

    rows.sort(key=lambda r: (r["gap"] is None, -(r["gap"] or 0.0)))
    return rows


def _join_roster_features(week_rosters, features_full):
    """This week's roster's rows from last week's usage table, keyed
    `player_id == espn_player_id`."""
    if week_rosters.empty or features_full.empty:
        return pd.DataFrame()
    ids = set(week_rosters["player_id"])
    return features_full[features_full["espn_player_id"].isin(ids)].reset_index(drop=True)


FOOTER_NOTES = [
    "The Thursday-night practice trajectory is two days deep (Wed/Thu), not three -- a short week "
    "gives genuinely less signal than Sunday's players will have by Friday.",
    "The market section's `fantasy_points` sums whatever markets this book actually quoted for a "
    "player -- a player quoted on only one market is understated relative to one quoted on several; "
    "see `markets` in the table.",
    "Divergence between prop points and ESPN's own projection is a tiebreaker to investigate, never "
    "an override -- the two numbers are built from different inputs and disagreement is expected.",
    "No drop candidates today. The waiver deadline was Tuesday night; a mid-week drop on two practice "
    "days trades a real bye-week problem for a marginal add.",
    "Report filenames carry the render date and week, not the covered one -- the usage section above "
    "covers week - 1 while this file is filed under week.",
]


def render(season, week, team_id, usage_week, thursday_games, tnf_rows, watch_rows,
           gate, features_df, props_gate, market, divergence_rows, could_not_compare,
           swap_rows, practice_df, footer_notes, window=None, rendered_at=None):
    """Pure over its arguments -- no disk access beyond the freshness header,
    same contract as wednesday.render/waivers.render."""
    rendered_at = rendered_at if rendered_at is not None else time.time()
    title = f"Usage and market -- {season} week {week}"
    covers = f"week {week}'s Thursday-night start/sit; canonical usage below reviews week {usage_week}"

    lines = header_lines(title, week, covers, window, rendered_at)
    lines.append("")

    lines.append("## Freshness")
    lines.extend(freshness_lines(freshness(season=season)))
    lines.append("")

    lines.append("## Decisions due")
    lines.append("**The Thursday-night start/sit is binding at kickoff** -- the only hard call today.")
    if thursday_games.empty:
        lines.append(f"No Thursday-night game in week {week}.")
    else:
        for _, game in thursday_games.iterrows():
            lines.append(f"- {game['away_team']} @ {game['home_team']}, {game.get('gametime', '')} ET")
        lines.append("")
        if not tnf_rows:
            lines.append("No rostered player is on tonight's two teams.")
        else:
            headers = ["player", "role", "slot", "tier", "practice (W/T/--)", "week_projected", "prop points"]
            rows = [
                [r["player_name"], r["side"], r["slot"], r["tier"] or INSUFFICIENT_DATA,
                 r["practice_trajectory"] or "-- / -- / --", num(r["week_projected"]), num(r["prop_points"])]
                for r in tnf_rows
            ]
            lines.extend(table(headers, rows))
            lines.append("")
            if watch_rows:
                lines.append("At-risk starters on tonight's teams:")
                for r in watch_rows:
                    repl = r["replacement"]
                    if repl:
                        lines.append(
                            f"- {r['player_name']} ({r['slot']}, {r['tier']}) -- best legal swap: "
                            f"{repl['player_name']} ({num(repl['projection'])} proj, tier={repl['tier']})"
                        )
                    else:
                        lines.append(f"- {r['player_name']} ({r['slot']}, {r['tier']}) -- no legal swap exists.")
    lines.append("")

    lines.append(f"## Canonical usage -- week {usage_week}")
    if gate["insufficient"]:
        lines.append(f"**{INSUFFICIENT_DATA}** -- {gate['reason']}")
    else:
        if gate["provisional"]:
            lines.append(
                f"_These figures are still **provisional** for week {usage_week} -- the Thursday "
                "nflverse `--force` refresh has not fully landed. Read them as a preview, not the "
                "canonical numbers._"
            )
        if features_df.empty:
            lines.append(f"**{INSUFFICIENT_DATA}** -- no rostered player matched to a week {usage_week} usage row.")
        else:
            headers = ["player", "pos", "offense_pct", "snap_pct_delta_1w", "snap_pct_delta_3w",
                       "snap_pct_trend", "targets", "target_share", "air_yards_share", "wopr",
                       "targets_per_snap"]
            rows = []
            for _, r in features_df.iterrows():
                offense_pct = None if r.get("bye") else _round_or_none(r.get("offense_pct"))
                rows.append([
                    r.get("player_name"), r.get("position"), offense_pct,
                    _round_or_none(r.get("snap_pct_delta_1w")), _round_or_none(r.get("snap_pct_delta_3w")),
                    _round_or_none(r.get("snap_pct_trend"), digits=4), _int_or_none(r.get("targets")),
                    _round_or_none(r.get("target_share")), _round_or_none(r.get("air_yards_share")),
                    _round_or_none(r.get("wopr")), _round_or_none(r.get("targets_per_snap")),
                ])
            lines.extend(table(headers, rows))
            byes = features_df[features_df["bye"] == True]  # noqa: E712
            if not byes.empty:
                lines.append("")
                lines.append("_On bye: " + ", ".join(sorted(byes["player_name"].dropna())) + "._")
    lines.append("")

    lines.append("## Market -- prop-derived points")
    if props_gate["insufficient"]:
        lines.append(f"**{INSUFFICIENT_DATA}** -- {props_gate['reason']}")
    else:
        by_player = market["by_player"]
        if not by_player:
            lines.append(f"**{INSUFFICIENT_DATA}** -- no props row resolved to a scoreable player.")
        else:
            headers = ["player", "prop points", "markets"]
            rows = [
                [info["player_name"], num(info["points"]), info["markets"]]
                for _, info in sorted(by_player.items(), key=lambda kv: kv[1]["points"], reverse=True)
            ]
            lines.extend(table(headers, rows))
        lines.append("")
        lines.append(
            f"_{market['unmatched_count']} props row(s) carried no ESPN player id/team match and are "
            "excluded from the table above -- kept, never dropped, in the underlying capture. The odds "
            "feed carries no player id and often no team, the one join in this repo with nothing to "
            "fall back on._"
        )
    lines.append("")

    lines.append("## Divergence -- prop points vs ESPN projection")
    if not divergence_rows:
        lines.append(
            f"No player's prop-derived total diverges from ESPN's projection by "
            f"{PROJECTION_DIVERGENCE_PCT:.0%} or more."
        )
    else:
        headers = ["player", "prop points", "ESPN projected", "divergence"]
        rows = [
            [r["player_name"], num(r["prop_total"]), num(r["espn_projected"]), f"{r['pct'] * 100:.0f}%"]
            for r in divergence_rows
        ]
        lines.extend(table(headers, rows))
    lines.append(f"_{could_not_compare} player(s) could not be compared -- zero or missing ESPN projection._")
    lines.append("")

    lines.append("## Swap candidates")
    if not swap_rows:
        lines.append("No starter's one-week snap share fell against a bench player trending the other way.")
    else:
        headers = ["starter", "starter delta", "starter wopr", "bench", "bench delta", "bench wopr", "gap"]
        rows = [
            [r["starter_name"], _round_or_none(r["starter_delta"]), _round_or_none(r["starter_wopr"]),
             r["bench_name"], _round_or_none(r["bench_delta"]), _round_or_none(r["bench_wopr"]), num(r["gap"])]
            for r in swap_rows
        ]
        lines.extend(table(headers, rows))
    lines.append("")

    lines.append("## Drop candidates")
    lines.append(
        "_None today._ The waiver deadline was Tuesday night; a mid-week drop on two practice days "
        "trades a real bye-week problem for a marginal add."
    )
    lines.append("")

    lines.append("## Practice report -- Wed / Thu")
    if practice_df.empty:
        lines.append(f"**{INSUFFICIENT_DATA}** -- no roster or no Sleeper snapshot to read.")
    else:
        headers = ["player", "trajectory (W/T/--)"]
        rows = [
            [r.get("player_name"), r.get("practice_trajectory") or "-- / -- / --"]
            for _, r in practice_df.iterrows()
        ]
        lines.extend(table(headers, rows))
    lines.append("")

    lines.append("## What this report cannot see")
    lines.extend(f"- {n}" for n in footer_notes)
    return "\n".join(lines) + "\n"


def payload(season, week, team_id, usage_week, thursday_games, tnf_rows, watch_rows,
            gate, features_df, props_gate, market, divergence_rows, could_not_compare,
            swap_rows, practice_df, footer_notes, window=None, rendered_at=None):
    """The structured twin of `render`, over the identical argument list.

    Note the two headings this report builds at render time -- "Canonical
    usage -- week N" -- which is why section ids here are fixed strings and
    never slugged from the heading. See espn_ff/report/payload.py.
    """
    rendered_at = rendered_at if rendered_at is not None else time.time()
    title = f"Usage and market -- {season} week {week}"
    covers = f"week {week}'s Thursday-night start/sit; canonical usage below reviews week {usage_week}"

    header = payload_lib.header_block(title, week, covers, window, rendered_at)
    sections = [
        payload_lib.freshness_section(freshness(season=season)),
        _decisions_section(week, thursday_games, tnf_rows, watch_rows),
        _canonical_usage_section(usage_week, gate, features_df),
        _market_section(props_gate, market),
        _divergence_section(divergence_rows, could_not_compare),
    ]

    if not swap_rows:
        sections.append(payload_lib.prose_section(
            "swap-candidates", "Swap candidates",
            ["No starter's one-week snap share fell against a bench player trending the other way."],
            data={"count": 0},
        ))
    else:
        columns = [
            payload_lib.column("starter_name", "starter", "string"),
            payload_lib.column("starter_delta", "starter delta", "number"),
            payload_lib.column("starter_wopr", "starter wopr", "number"),
            payload_lib.column("bench_name", "bench", "string"),
            payload_lib.column("bench_delta", "bench delta", "number"),
            payload_lib.column("bench_wopr", "bench wopr", "number"),
            payload_lib.column("gap", "gap", "number"),
        ]
        sections.append(payload_lib.table_section(
            "swap-candidates", "Swap candidates", columns,
            payload_lib.rows(swap_rows, columns), data={"count": len(swap_rows)},
        ))

    sections.append(payload_lib.prose_section(
        "drop-candidates", "Drop candidates",
        ["_None today._ The waiver deadline was Tuesday night; a mid-week drop on two practice days "
         "trades a real bye-week problem for a marginal add."],
        data={"by_design": True},
    ))

    if practice_df.empty:
        sections.append(payload_lib.insufficient_section(
            "practice-report", "Practice report -- Wed / Thu",
            "no roster or no Sleeper snapshot to read",
        ))
    else:
        columns = [
            payload_lib.column("player_name", "player", "string"),
            payload_lib.column("practice_trajectory", "trajectory (W/T/--)", "string"),
        ]
        sections.append(payload_lib.table_section(
            "practice-report", "Practice report -- Wed / Thu", columns,
            payload_lib.rows(practice_df, columns),
        ))

    sections.append(payload_lib.list_section("cannot-see", "What this report cannot see", footer_notes))
    return header, sections


def _decisions_section(week, thursday_games, tnf_rows, watch_rows):
    """`## Decisions due` -- the only binding call of the day, so the lead
    line is flagged `emphasis` and the binding fact rides in `data`."""
    lead = payload_lib.prose_section(
        "decisions-due-lead", None,
        ["**The Thursday-night start/sit is binding at kickoff** -- the only hard call today."],
        emphasis=True, level=None,
    )
    if thursday_games.empty:
        return payload_lib.blocks_section(
            "decisions-due", "Decisions due",
            [lead, payload_lib.prose_section(
                "tnf-game", None, [f"No Thursday-night game in week {week}."], level=None,
            )],
            data={"binding": True, "has_game": False, "tnf_player_count": 0},
        )

    game_columns = [
        payload_lib.column("away_team", "away", "string"),
        payload_lib.column("home_team", "home", "string"),
        payload_lib.column("gametime", "gametime", "string"),
    ]
    blocks = [lead, payload_lib.table_section(
        "tnf-game", None, game_columns, payload_lib.rows(thursday_games, game_columns), level=None,
    )]

    if not tnf_rows:
        blocks.append(payload_lib.prose_section(
            "tnf-players", None, ["No rostered player is on tonight's two teams."], level=None,
        ))
    else:
        columns = [
            payload_lib.column("player_name", "player", "string"),
            payload_lib.column("side", "role", "string"),
            payload_lib.column("slot", "slot", "string"),
            payload_lib.column("tier", "tier", "string"),
            payload_lib.column("practice_trajectory", "practice (W/T/--)", "string"),
            payload_lib.column("week_projected", "week_projected", "number"),
            payload_lib.column("prop_points", "prop points", "number"),
        ]
        blocks.append(payload_lib.table_section(
            "tnf-players", None, columns, payload_lib.rows(tnf_rows, columns), level=None,
        ))
        if watch_rows:
            swap_columns = [
                payload_lib.column("player_name", "player", "string"),
                payload_lib.column("slot", "slot", "string"),
                payload_lib.column("tier", "tier", "string"),
                payload_lib.column("replacement_name", "best legal swap", "string"),
                payload_lib.column("replacement_projection", "swap proj", "number"),
                payload_lib.column("replacement_tier", "swap tier", "string"),
            ]
            rows = []
            for r in watch_rows:
                repl = r["replacement"]
                rows.append({
                    "player_name": payload_lib.unset(r["player_name"]),
                    "slot": payload_lib.unset(r["slot"]), "tier": payload_lib.unset(r["tier"]),
                    "replacement_name": payload_lib.unset(repl["player_name"]) if repl else None,
                    "replacement_projection": payload_lib.unset(repl["projection"]) if repl else None,
                    "replacement_tier": payload_lib.unset(repl["tier"]) if repl else None,
                })
            blocks.append(payload_lib.table_section(
                "tnf-at-risk", None, swap_columns, rows,
                notes=["At-risk starters on tonight's teams:"], level=None,
            ))

    return payload_lib.blocks_section(
        "decisions-due", "Decisions due", blocks,
        data={"binding": True, "has_game": True, "tnf_player_count": len(tnf_rows),
              "at_risk_count": len(watch_rows)},
    )


def _canonical_usage_section(usage_week, gate, features_df):
    heading = f"Canonical usage -- week {usage_week}"
    if gate["insufficient"]:
        return payload_lib.insufficient_section(
            "canonical-usage", heading, gate["reason"], data={"usage_week": usage_week},
        )
    if features_df.empty:
        return payload_lib.insufficient_section(
            "canonical-usage", heading,
            f"no rostered player matched to a week {usage_week} usage row",
            data={"usage_week": usage_week, "provisional": gate["provisional"]},
        )

    notes = []
    if gate["provisional"]:
        notes.append(
            f"_These figures are still **provisional** for week {usage_week} -- the Thursday "
            "nflverse `--force` refresh has not fully landed. Read them as a preview, not the "
            "canonical numbers._"
        )
    byes = features_df[features_df["bye"] == True]  # noqa: E712
    if not byes.empty:
        notes.append("_On bye: " + ", ".join(sorted(byes["player_name"].dropna())) + "._")

    columns = [
        payload_lib.column("player_name", "player", "string"),
        payload_lib.column("position", "pos", "string"),
        payload_lib.column("offense_pct", "offense_pct", "number"),
        payload_lib.column("snap_pct_delta_1w", "snap_pct_delta_1w", "number"),
        payload_lib.column("snap_pct_delta_3w", "snap_pct_delta_3w", "number"),
        payload_lib.column("snap_pct_trend", "snap_pct_trend", "number"),
        payload_lib.column("targets", "targets", "integer"),
        payload_lib.column("target_share", "target_share", "number"),
        payload_lib.column("air_yards_share", "air_yards_share", "number"),
        payload_lib.column("wopr", "wopr", "number"),
        payload_lib.column("targets_per_snap", "targets_per_snap", "number"),
        payload_lib.column("bye", "bye", "boolean"),
    ]
    rows = []
    for _, r in features_df.iterrows():
        row = {col["key"]: payload_lib.unset(r.get(col["key"])) for col in columns}
        # Same suppression render applies: a bye week's snap share is not 0,
        # it is undefined, and a consumer plotting it would draw a cliff.
        if r.get("bye"):
            row["offense_pct"] = None
        rows.append(row)

    return payload_lib.table_section(
        "canonical-usage", heading, columns, rows, notes=notes,
        data={"usage_week": usage_week, "provisional": gate["provisional"], "bye_count": len(byes)},
    )


def _market_section(props_gate, market):
    if props_gate["insufficient"]:
        return payload_lib.insufficient_section(
            "market", "Market -- prop-derived points", props_gate["reason"],
        )
    by_player = market["by_player"]
    note = (
        f"_{market['unmatched_count']} props row(s) carried no ESPN player id/team match and are "
        "excluded from the table above -- kept, never dropped, in the underlying capture. The odds "
        "feed carries no player id and often no team, the one join in this repo with nothing to "
        "fall back on._"
    )
    if not by_player:
        return payload_lib.insufficient_section(
            "market", "Market -- prop-derived points",
            "no props row resolved to a scoreable player",
            data={"unmatched_count": market["unmatched_count"]},
        )
    columns = [
        payload_lib.column("player_name", "player", "string"),
        payload_lib.column("points", "prop points", "number"),
        payload_lib.column("markets", "markets", "string"),
    ]
    rows = [
        {"player_name": payload_lib.unset(info["player_name"]),
         "points": payload_lib.unset(info["points"]), "markets": payload_lib.unset(info["markets"])}
        for _, info in sorted(by_player.items(), key=lambda kv: kv[1]["points"], reverse=True)
    ]
    return payload_lib.table_section(
        "market", "Market -- prop-derived points", columns, rows, notes=[note],
        data={"unmatched_count": market["unmatched_count"]},
    )


def _divergence_section(divergence_rows, could_not_compare):
    note = f"_{could_not_compare} player(s) could not be compared -- zero or missing ESPN projection._"
    heading = "Divergence -- prop points vs ESPN projection"
    if not divergence_rows:
        return payload_lib.prose_section(
            "divergence", heading,
            [f"No player's prop-derived total diverges from ESPN's projection by "
             f"{PROJECTION_DIVERGENCE_PCT:.0%} or more.", note],
            data={"count": 0, "could_not_compare": could_not_compare,
                  "threshold_pct": PROJECTION_DIVERGENCE_PCT},
        )
    columns = [
        payload_lib.column("player_name", "player", "string"),
        payload_lib.column("prop_total", "prop points", "number"),
        payload_lib.column("espn_projected", "ESPN projected", "number"),
        payload_lib.column("pct", "divergence", "number"),
    ]
    # `pct` stays a fraction, as the module computes it. The markdown prints
    # it as a rounded percentage string, which is lossy in both directions.
    return payload_lib.table_section(
        "divergence", heading, columns, payload_lib.rows(divergence_rows, columns), notes=[note],
        data={"count": len(divergence_rows), "could_not_compare": could_not_compare,
              "threshold_pct": PROJECTION_DIVERGENCE_PCT},
    )


def build(season, week, team_id=None):
    """Assemble the full Thursday report as markdown text. `week` is the
    current scoring period; usage internally reviews `week - 1`."""
    team_id = team_id if team_id is not None else config.TEAM_ID
    rendered_at = time.time()
    usage_week = week - 1

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

    thursday_games = schedule.remaining_games(season, week, weekday="Thursday")
    thursday_teams = schedule.teams_in(thursday_games)

    # Starters + bench (never IR -- tuesday._bench already excludes it, and
    # an IR player is not a start/sit decision) whose pro_team plays tonight.
    roster_pool = pd.concat([starters, bench]) if not week_rosters.empty else week_rosters
    tnf_roster = roster_pool[roster_pool["pro_team"].isin(thursday_teams)] if thursday_teams else roster_pool.iloc[0:0]

    avail_df = availability.read(week_rosters, season, week) if not week_rosters.empty else pd.DataFrame()
    avail_by_id = dict(zip(avail_df["player_id"], avail_df["tier"])) if not avail_df.empty else {}

    today = pd.Timestamp(rendered_at, unit="s", tz="UTC").tz_convert(weeks.ET).date()
    signals_df = wednesday.practice_signals(week_rosters, today=today) if not week_rosters.empty else pd.DataFrame()
    signals_by_id = {r["player_id"]: r for _, r in signals_df.iterrows()} if not signals_df.empty else {}
    practice_df = (
        signals_df.merge(week_rosters[["player_id", "player_name"]], on="player_id", how="left")
        if not signals_df.empty else signals_df
    )

    features_full = loaders.features(season, usage_week)
    gate = canonical_read(features_full, usage_week)
    our_features = _join_roster_features(week_rosters, features_full) if not gate["insufficient"] else pd.DataFrame()
    features_by_id = (
        {r["espn_player_id"]: r for _, r in features_full.iterrows() if pd.notna(r.get("espn_player_id"))}
        if not gate["insufficient"] else {}
    )

    props_gate = props_read(week)
    market = (
        market_points(props_gate["props_df"], pool_df) if not props_gate["insufficient"]
        else {"by_player": {}, "unmatched_count": 0}
    )
    divergence_rows, could_not_compare = divergence(market["by_player"], pool_df)

    watch_rows = []
    if not tnf_roster.empty:
        for _, starter in tnf_roster[tnf_roster["started"]].iterrows():
            tier = avail_by_id.get(starter["player_id"])
            if not wednesday.is_at_risk(tier):
                continue
            replacement, _, _ = wednesday.best_replacement(starter, bench, pool_df, allowed_slots, avail_by_id)
            watch_rows.append({
                "player_name": starter["player_name"], "slot": starter["lineup_slot"],
                "tier": tier, "replacement": replacement,
            })

    tnf_rows = []
    if not tnf_roster.empty:
        for _, r in tnf_roster.iterrows():
            signal = signals_by_id.get(r["player_id"], {})
            proj, _ = waivers.week_projection(r["player_id"], pool_df, r)
            m = market["by_player"].get(r["player_id"])
            tnf_rows.append({
                "player_name": r["player_name"], "side": "starter" if r["started"] else "bench",
                "slot": r["lineup_slot"], "tier": avail_by_id.get(r["player_id"]),
                "practice_trajectory": signal.get("practice_trajectory"),
                "week_projected": proj, "prop_points": m["points"] if m else None,
            })

    swap_rows = swap_candidates(starters, bench, features_by_id, pool_df, allowed_slots)

    footer_notes = list(FOOTER_NOTES)
    for name, (_, stale) in freshness(season=season).items():
        if stale:
            footer_notes.append(f"The {name} feed is stale as of this report's generation.")
    export_warning = espn_export_warning()
    if export_warning:
        footer_notes.append(f"The last ESPN export shrank -- {export_warning}.")
    # Per-view, not the feed-level `freshness()` loop above: that one reads
    # max(fetched_at) across every cached ESPN view, so a fresh player-pool
    # fetch masks a day-old roster. See loaders.roster_read_is_current.
    roster_note = roster_staleness_note(rendered_at, season=season, week=week)
    if roster_note:
        footer_notes.append(roster_note)
    if gate["insufficient"]:
        footer_notes.append(f"Canonical usage could not be read -- {gate['reason']}.")
    elif gate["provisional"]:
        footer_notes.append(f"Canonical usage for week {usage_week} is still provisional as of this render.")
    if props_gate["insufficient"]:
        footer_notes.append(f"The market section could not be read -- {props_gate['reason']}.")
    else:
        if market["unmatched_count"]:
            footer_notes.append(
                f"{market['unmatched_count']} props row(s) could not be matched to an ESPN player -- "
                "counted, never dropped."
            )
        single_market = [pid for pid, info in market["by_player"].items() if info["markets"] == 1]
        if single_market:
            footer_notes.append(
                f"{len(single_market)} player(s) above are priced on a single market -- their prop "
                "total is a floor, not a full number."
            )
    footer_notes.append(
        f"PROJECTION_DIVERGENCE_PCT ({PROJECTION_DIVERGENCE_PCT:.0%}) is chosen, not fitted."
    )
    if could_not_compare:
        footer_notes.append(
            f"{could_not_compare} player(s) could not be compared for divergence -- zero or missing "
            "ESPN projection."
        )

    args = (
        season, week, team_id, usage_week, thursday_games, tnf_rows, watch_rows,
        gate, our_features, props_gate, market, divergence_rows, could_not_compare,
        swap_rows, practice_df, footer_notes,
    )
    kwargs = {"window": weeks.week_window(season, week), "rendered_at": rendered_at}
    header, sections = payload(*args, **kwargs)
    return payload_lib.RenderedReport(
        render(*args, **kwargs), {"header": header, "sections": sections}
    )
