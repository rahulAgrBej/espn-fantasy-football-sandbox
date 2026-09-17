"""Sunday -- the pre-lock call.

The last report before the lock, and the only one whose value decays by the
minute: at 13:00 ET it stops being a decision and becomes history. See
docs/report-weekly-schedule.md's "Sunday -- pre-lock call" section for the
full spec this module implements.

**What Sunday can actually read.** Odds `pre_lock` (10:38 ET) -- the only job
permitted to draw the credit reserve down to zero, and the only one that
writes both team_totals.parquet and player_props.parquet in a single run;
Sleeper's daily slim snapshot (08:11 ET); nflverse's routine pull (09:23 ET).
ESPN's last pull is still Wednesday's -- `espn-sunday-live` does not start
until 13:08, after the lock -- so the lineup solved below is solved against a
four-day-old weekly-rosters.csv, one day worse than the gap saturday.py
already documents.

**The one check the spec mandates.** A budget-aborted `pre_lock` writes a
fresh `ran_at` over unchanged data, leaving Friday's `line_movement` lines on
disk looking identical to a fresh pull. `pre_lock_read` is the gate that
catches it, and `featured_lines`/`pre_lock_props` carry a `from_this_run`
verdict comparing last_run's `ran_at` against the newest `captured_at` --
neither parquet carries a job-name column, so timestamp agreement is the
strongest instrument available *(Inferred)*.

**Friday's held-open slots are recomputed, never read.** Nothing persists
`friday.held_open_slots`, so this module reruns `friday.recommended_lineup`
and `friday.held_open_slots` against this morning's data -- the precedent
`saturday.swap_pairs` set. They are labelled "still undecided as of this
morning", not "what Friday held": the two agree whenever no input has moved.

Reuse-first, like every other day module: `friday.recommended_lineup`/
`held_open_slots`/`_best_alternative`, `saturday.baseline_snapshot`/
`tier_diff`/`swap_pairs`, `thursday.market_points`, `wednesday.best_replacement`/
`is_at_risk`/`practice_signals`, `tuesday._bench`/`slot_map_divergence`,
`monday.league_slots`, `schedule.remaining_games`, and
`odds_projections.team_totals_by_capture`/`props_by_capture`.
"""

import time

import pandas as pd

from .. import config, weeks
from ..odds import projections as odds_projections
from ..odds import store as odds_store
from . import availability, friday, monday, payload as payload_lib, saturday, schedule, thursday, tuesday, wednesday
from .loaders import espn_export_warning, freshness, latest_export
from .render import INSUFFICIENT_DATA, freshness_lines, header_lines, num, table

_PRE_LOCK_JOB = "pre_lock"

# The spec's stated deadline, used only when this week's Sunday slate cannot
# be read out of nflverse -- never in preference to it.
LOCK_DEADLINE_ET_HOUR = 13

# How close last_run's `ran_at` and a parquet's newest `captured_at` must be
# to read as "the same run". odds/jobs.py:pre_lock derives both from one
# `now`, so a real run makes them the same instant; the window is slack for
# clock skew between the write calls, not a tolerance for a different job.
_CAPTURE_MATCH_TOLERANCE_S = 300

_OUT_TIER = "OUT"


def _et_date(ts):
    return pd.Timestamp(ts, unit="s", tz="UTC").tz_convert(weeks.ET).date()


def _fmt_et(ts):
    if ts is None:
        return "never"
    return pd.Timestamp(ts, unit="s", tz="UTC").tz_convert(weeks.ET).strftime("%Y-%m-%d %H:%M ET")


def _fmt_captured_at(value):
    if value is None:
        return INSUFFICIENT_DATA
    return pd.Timestamp(value).tz_convert(weeks.ET).strftime("%a %H:%M ET")


def _is_from_this_run(captured_at, ran_at):
    """Whether `captured_at` and `ran_at` name the same run. Inferred, not
    Documented: no parquet carries a job-name column, so this is the
    strongest available answer to "did pre_lock write this row", and it
    degrades if two odds jobs ever land inside the tolerance window."""
    if captured_at is None or ran_at is None:
        return False
    return abs(pd.Timestamp(captured_at).timestamp() - ran_at) <= _CAPTURE_MATCH_TOLERANCE_S


def pre_lock_read(today=None):
    """{"insufficient", "reason", "ran_at", "credits_spent", "stale_flag"} --
    the same gate shape as thursday.props_read / friday.line_movement_read /
    waivers.team_totals, and the one check the spec names outright: "the
    report's only job is to verify it actually ran."

    A reason-string ladder: no `pre_lock` entry in last_run.json at all;
    that entry's own `stale` flag set (the budget-abort case
    docs/odds-budget.md owns); the entry's `ran_at` is not today's ET date.

    That third rung exists in no other module and is the subtle one. A
    `pre_lock` entry from last Sunday carries `stale = False` and is
    indistinguishable from this morning's by every field except its date --
    only the date separates "the market moved" from "you are reading a
    week-old line".

    Takes no `week`: this gate answers "did the job run", and the frames
    answer "for which week". Carrying an unused argument to look like its
    three siblings would be worse than the asymmetry.
    """
    empty = {"insufficient": True, "ran_at": None, "credits_spent": None, "stale_flag": None}
    last_run = odds_store.read_last_run(_PRE_LOCK_JOB)
    if last_run is None:
        return {
            **empty,
            "reason": (
                f"no {_PRE_LOCK_JOB} entry in last_run.json -- this morning's 10:38 ET run has not "
                "landed, and every market figure below would be Friday's line_movement capture "
                "wearing today's date"
            ),
        }

    ran_at = last_run.get("ran_at")
    if last_run.get("stale"):
        return {
            **empty, "ran_at": ran_at, "stale_flag": True,
            "reason": f"{_PRE_LOCK_JOB}'s last run is marked stale: {last_run.get('reason')}",
        }

    today = today if today is not None else _et_date(time.time())
    if ran_at is None or _et_date(ran_at) != today:
        return {
            **empty, "ran_at": ran_at, "stale_flag": False,
            "reason": (
                f"{_PRE_LOCK_JOB} last ran {_fmt_et(ran_at)} -- not today, so the lines below are a "
                "prior week's, not this morning's"
            ),
        }

    return {
        "insufficient": False, "reason": None, "ran_at": ran_at,
        "credits_spent": last_run.get("credits_spent"), "stale_flag": False,
    }


def featured_lines(week, gate):
    """{"insufficient", "reason", "rows", "current_at", "prior_at",
    "from_this_run"} -- `pre_lock`'s featured spreads and totals, with the
    delta against the previous capture.

    Reuses `odds_projections.team_totals_by_capture`, the per-capture
    accessor Friday added. **One capture is not insufficient here**, which is
    the deliberate divergence from `friday.line_movement_read`: Friday's
    section *is* the diff, so a single capture has nothing to say, while this
    section is "the pre_lock job's featured lines" -- the absolute numbers
    are the deliverable and the delta is garnish. A lone capture renders its
    rows with `implied_delta` None, which render.table already prints as
    `--`.

    `prior_at` is `captures[-2]`, not `captures[0]`: by Sunday this parquet
    holds three captures for the week (Tuesday's slate, Friday's
    line_movement, this morning's pre_lock), and diffing against the earliest
    would render "movement since Tuesday's open" under a Sunday heading.
    """
    empty = {"insufficient": True, "rows": [], "current_at": None, "prior_at": None, "from_this_run": False}
    if gate["insufficient"]:
        return {**empty, "reason": gate["reason"]}
    if not config.ODDS_TEAM_TOTALS.exists():
        return {**empty, "reason": "no team_totals.parquet on disk"}

    totals_df = odds_projections.team_totals_by_capture(week=week)
    if totals_df.empty:
        return {**empty, "reason": f"no team_totals rows for week {week}"}

    captures = sorted(totals_df["captured_at"].unique())
    current_at = captures[-1]
    prior_at = captures[-2] if len(captures) >= 2 else None
    current_df = totals_df[totals_df["captured_at"] == current_at]

    if prior_at is None:
        rows = [
            {
                "team": r["team"], "spread": r["spread"], "total": r["total"],
                "implied": r["implied_team_total"], "spread_prior": None,
                "implied_prior": None, "implied_delta": None,
            }
            for _, r in current_df.iterrows()
        ]
    else:
        prior_df = totals_df[totals_df["captured_at"] == prior_at]
        merged = current_df.merge(prior_df, on="team", how="left", suffixes=("_now", "_prior"))
        rows = []
        for _, r in merged.iterrows():
            implied_prior = r.get("implied_team_total_prior")
            delta = None if pd.isna(implied_prior) else r["implied_team_total_now"] - implied_prior
            rows.append({
                "team": r["team"], "spread": r["spread_now"], "total": r["total_now"],
                "implied": r["implied_team_total_now"],
                "spread_prior": r.get("spread_prior"),
                "implied_prior": None if pd.isna(implied_prior) else implied_prior,
                "implied_delta": delta,
            })

    rows.sort(key=lambda r: (r["implied_delta"] is None, r["implied_delta"] or 0.0))
    return {
        "insufficient": False, "reason": None, "rows": rows,
        "current_at": current_at, "prior_at": prior_at,
        "from_this_run": _is_from_this_run(current_at, gate["ran_at"]),
    }


def pre_lock_props(week, gate, pool_df):
    """{"insufficient", "reason", "captured_at", "by_player",
    "unmatched_count", "from_this_run"} -- this morning's props capture only,
    converted to fantasy points by `thursday.market_points` verbatim.

    `odds_projections.props_by_capture` is what makes the isolation possible:
    `build()` medians every capture of the week into one row, so Thursday's
    props_primary line would be folded into a figure this report calls "the
    pre_lock consensus". Only the newest capture is used.
    """
    empty = {
        "insufficient": True, "captured_at": None, "by_player": {},
        "unmatched_count": 0, "from_this_run": False,
    }
    if gate["insufficient"]:
        return {**empty, "reason": gate["reason"]}
    if not config.ODDS_PROPS.exists():
        return {**empty, "reason": "no player_props.parquet on disk"}

    props_df = odds_projections.props_by_capture(week=week)
    if props_df.empty:
        return {**empty, "reason": f"no props rows for week {week} in any capture"}

    captured_at = max(props_df["captured_at"])
    market = thursday.market_points(props_df[props_df["captured_at"] == captured_at], pool_df)
    return {
        "insufficient": False, "reason": None, "captured_at": captured_at,
        "by_player": market["by_player"], "unmatched_count": market["unmatched_count"],
        "from_this_run": _is_from_this_run(captured_at, gate["ran_at"]),
    }


def undecided_slots(week_rosters, pool_df, roster_slots_df, allowed_slots, avail_by_id, trajectory_by_id):
    """Returns (recommended, held_rows) -- a thin sequencer over Friday's own
    two functions, on purpose.

    This is the recompute the spec's "the specific slots Friday left open"
    resolves to. Nothing persists Friday's `held_open_slots` output; it
    exists only inside Friday's rendered markdown, and parsing that would
    couple this module to Friday's table formatting. Rerunning the same rule
    against this morning's data is what `saturday.swap_pairs` already does
    for its replacements. The two agree whenever no relevant input has moved
    since Friday; when one has, this morning's answer is the more current
    one, not a discrepancy to reconcile -- which is why `render` labels the
    section "still undecided as of this morning" and never "what Friday
    held".

    `recommended` is returned whole because `ranked_undecided` needs its
    candidates/eligibility/values working set and `render` needs its lineup.
    """
    recommended = friday.recommended_lineup(week_rosters, pool_df, roster_slots_df, allowed_slots)
    if recommended["insufficient"]:
        return recommended, []
    return recommended, friday.held_open_slots(recommended, avail_by_id, trajectory_by_id)


def _eligible_for_slot(slot, recommended, exclude_player_id=None):
    """Every candidate eligible for `slot`, with its projection --
    `friday._best_alternative`'s walk without the argmax, since this report
    ranks the whole set on a different key rather than taking the
    highest-projected one."""
    out = []
    candidates, eligibility, values = recommended["candidates"], recommended["eligibility"], recommended["values"]
    for i, c in enumerate(candidates):
        if c["player_id"] == exclude_player_id or slot not in eligibility[i]:
            continue
        out.append({"player_id": c["player_id"], "player_name": c["player_name"], "projection": values[i]})
    return out


def ranked_undecided(held_rows, recommended, market_by_player, avail_by_id):
    """Each undecided slot's candidates ranked by the `pre_lock` consensus,
    per the spec's "ranked by the pre_lock consensus".

    A candidate with no prop quote in this morning's capture is ranked on
    `week_projection` instead and sorts last, named in `no_market_names` --
    the counted-and-named convention rather than a coerced 0.0 that would
    silently rank them worst on merit.

    `friday._best_alternative` is called alongside, not instead: it answers
    "who would Friday's projection-only rule pick", the market ranking
    answers "who does this morning's market pick", and `agrees` makes a
    disagreement between them a finding. That disagreement is the entire
    reason this section renders at 11:30 rather than being settled at 11:00
    on Friday.
    """
    rows, no_market_names = [], set()
    for held in held_rows:
        slot = held["slot"]
        incumbent_id = next(
            (r["player_id"] for r in recommended["lineup"]
             if r["slot"] == slot and r["player_name"] == held["player_name"]),
            None,
        )
        candidates = []
        for c in _eligible_for_slot(slot, recommended):
            market = market_by_player.get(c["player_id"])
            if market is None:
                no_market_names.add(c["player_name"])
                rank_key, rank_source, markets = c["projection"], "espn", None
            else:
                rank_key, rank_source, markets = market["points"], "pre_lock", market["markets"]
            candidates.append({
                **c, "prop_points": None if market is None else market["points"],
                "markets": markets, "tier": avail_by_id.get(c["player_id"]),
                "rank_key": rank_key, "rank_source": rank_source,
            })
        candidates.sort(key=lambda c: (c["rank_source"] != "pre_lock", -(c["rank_key"] or 0.0)))

        market_best = candidates[0] if candidates and candidates[0]["rank_source"] == "pre_lock" else None
        friday_best = friday._best_alternative(
            slot, incumbent_id, recommended["candidates"], recommended["eligibility"], recommended["values"]
        )
        incumbent = next((c for c in candidates if c["player_id"] == incumbent_id), None)
        rows.append({
            "slot": slot, "reason": held["reason"], "incumbent": incumbent,
            "incumbent_name": held["player_name"], "candidates": candidates,
            "market_best": market_best, "friday_best": friday_best,
            "agrees": bool(market_best and friday_best and market_best["player_id"] == friday_best["player_id"]),
        })
    return rows, no_market_names


def overnight_out(week_rosters, starters, bench, pool_df, allowed_slots, avail_by_id, today,
                  snapshots_by_date=None, sleeper_df=None):
    """{"gate", "moved", "already_out", "unmatched_names", "fallback_names",
    "nan_names"} -- starters whose tier reads OUT, in two buckets.

    `moved` is the spec's "any starter whose tier moved to OUT overnight",
    via `saturday.baseline_snapshot` + `tier_diff` + `swap_pairs` reused
    whole. `already_out` is the case the spec's wording leaves out and this
    report cannot: a starter who was already OUT on Saturday is still a
    lineup you must not lock, and dropping them for reading the spec
    literally would be a real error dressed as fidelity. Both render in one
    table separated by a `since` column.

    HIGH_RISK-but-not-OUT is deliberately absent. `friday._TIER_DOWNGRADE`
    folds it in for a different purpose and Saturday's report owns the
    broader diff; here, an at-risk starter with a close alternative is
    already an undecided slot above, and a second table would say it twice.
    """
    gate = saturday.baseline_snapshot(today, snapshots_by_date=snapshots_by_date)
    empty = {
        "gate": gate, "moved": [], "already_out": [],
        "unmatched_names": set(), "fallback_names": set(), "nan_names": set(),
    }
    if gate["insufficient"] or week_rosters.empty:
        return empty

    snapshots_by_date = wednesday._snapshots_by_date() if snapshots_by_date is None else snapshots_by_date
    sleeper_df = availability.sleeper_by_espn_id() if sleeper_df is None else sleeper_df
    baseline_df = snapshots_by_date.get(gate["baseline_date"])
    current_df = snapshots_by_date.get(gate["current_date"])

    diff_rows, unmatched_names = saturday.tier_diff(week_rosters, baseline_df, current_df, sleeper_df)
    swap_rows, fallback_names, nan_names = saturday.swap_pairs(
        diff_rows, starters, bench, pool_df, allowed_slots, avail_by_id
    )

    starters_by_id = {r["player_id"]: r for _, r in starters.iterrows()} if not starters.empty else {}
    moved = [{**r, "since": "overnight"} for r in swap_rows if r["tier_now"] == _OUT_TIER]

    already_out = []
    for row in diff_rows:
        if row["changed"] or row["tier_now"] != _OUT_TIER:
            continue
        starter = starters_by_id.get(row["player_id"])
        if starter is None:
            continue
        replacement, fb, nan = wednesday.best_replacement(starter, bench, pool_df, allowed_slots, avail_by_id)
        fallback_names |= fb
        nan_names |= nan
        already_out.append({**row, "replacement": replacement, "since": "already OUT at the baseline"})

    return {
        "gate": gate, "moved": moved, "already_out": already_out,
        "unmatched_names": unmatched_names, "fallback_names": fallback_names, "nan_names": nan_names,
    }


def minutes_to_lock(season, week, rendered_at):
    """{"kickoff_at", "kickoff_label", "minutes", "source", "reason"} -- the
    instrument behind the spec's "carry its own generation timestamp
    prominently enough that a stale tab is obvious".

    The first Sunday kickoff comes from nflverse's own slate via
    `schedule.remaining_games(weekday="Sunday")`. When that cannot be read --
    an empty slate, an unparseable `gametime`, or `nflverse_store.load`
    raising because the required dataset is absent -- it falls back to the
    spec's 13:00 ET and says so. Catching here departs from
    `saturday.bye_week_starters`, which leaves the same exposure unguarded,
    and does so deliberately: this value feeds the report's first body line,
    and a header that can take the whole render down is worse than a header
    with a stated fallback.

    `minutes` is returned negative after kickoff rather than clamped -- the
    report says the thing rather than hiding it.

    nflverse's `gametime` is read as an ET wall clock. That is **Inferred**:
    the column carries no zone, and misreading it would move the stated
    deadline by hours, which is why it is a footer note and a known gap.
    """
    now = pd.Timestamp(rendered_at, unit="s", tz="UTC").tz_convert(weeks.ET)
    fallback_at = now.normalize() + pd.Timedelta(hours=LOCK_DEADLINE_ET_HOUR)

    def _fallback(reason):
        return {
            "kickoff_at": fallback_at, "kickoff_label": f"{LOCK_DEADLINE_ET_HOUR}:00 ET",
            "minutes": int((fallback_at - now).total_seconds() // 60),
            "source": "the spec's 13:00 ET default", "reason": reason,
        }

    try:
        games = schedule.remaining_games(season, week, weekday="Sunday")
    except Exception as exc:  # nflverse schedules is a required dataset; load raises when absent
        return _fallback(f"nflverse schedules could not be loaded ({exc})")

    if games.empty:
        return _fallback(f"nflverse lists no Sunday game for {season} week {week}")

    stamps = pd.to_datetime(
        games["gameday"].astype(str) + " " + games["gametime"].astype(str), errors="coerce"
    ).dropna()
    if stamps.empty:
        return _fallback("no Sunday game carried a parseable gameday/gametime")

    kickoff_at = stamps.min().tz_localize(weeks.ET)
    return {
        "kickoff_at": kickoff_at, "kickoff_label": kickoff_at.strftime("%H:%M ET"),
        "minutes": int((kickoff_at - now).total_seconds() // 60),
        "source": "nflverse schedules", "reason": None,
    }


FOOTER_NOTES = [
    # Kept word-for-word compatible with saturday.FOOTER_NOTES's own inactives
    # line, extended -- the two must not drift into different claims.
    "Official inactives drop roughly 90 minutes before kickoff and appear in no feed this pipeline "
    "touches -- a report generated at 11:30 ET cannot see them, and nothing above substitutes for "
    "checking them yourself before kickoff.",
    "ESPN last pulled Wednesday (`espn-wednesday`, 09:08 ET); `espn-sunday-live` does not start until "
    "13:08, after the lock. Every roster, IR and lineup-slot move made Thursday through this morning "
    "is invisible, and the lineup above is solved against Wednesday's weekly-rosters.csv.",
    "The undecided slots above are recomputed against this morning's data by the same "
    "`friday.recommended_lineup`/`friday.held_open_slots` this week's Friday report ran. Nothing "
    "persists Friday's own computation. The two agree whenever no input has moved since Friday; when "
    "one has, this morning's answer is the more current one, not a discrepancy to reconcile.",
    "`pre_lock` is the only odds job permitted to draw down the reserve; `docs/odds-budget.md` owns "
    "those rules. This report neither enforces nor estimates that budget -- it only verifies the job "
    "ran.",
    "`pre_lock` pulls props with `commence_after=now` at 10:38, so a game already kicked off by then "
    "carries no props here -- an absence in the market table is not evidence of no quote. The capture "
    "carries no kickoff time and `props_by_capture` drops `event_id`, so a game kicking off between "
    "10:38 and this render can still appear under a heading that says \"still undecided\".",
    "A player quoted on one market has an understated market total -- the markets column says how "
    "many were summed.",
    "The kickoff clock reads nflverse's `gameday`/`gametime` as an ET wall clock, which nflverse does "
    "not publish a zone for (Inferred). When the slate cannot be read at all the deadline shown is "
    "the spec's 13:00 ET default, labelled as such.",
    "Ranking undecided slots by the `pre_lock` consensus rather than ESPN's projection is a chosen "
    "rule, not a fitted one -- no report in this repo is ever scored against what actually happened.",
    "This filename carries the render date, not the covered one, same convention as every other "
    "report.",
]


def render(season, week, team_id, lock, gate, lines, props, market_by_player, undecided_rows,
           out_read, recommended, footer_notes, window=None, rendered_at=None):
    """Pure over its arguments -- no disk access beyond the freshness
    header, same contract as every other day module."""
    rendered_at = rendered_at if rendered_at is not None else time.time()
    title = f"Final lock -- {season} week {week}"
    covers = f"week {week}'s Sunday lineup lock, before {lock['kickoff_label']}"

    lines_out = header_lines(title, week, covers, window, rendered_at)
    lines_out.append("")

    # The spec's prominence requirement: the first body line, above even the
    # freshness block, so a tab left open past kickoff says so at a glance.
    if lock["minutes"] < 0:
        lines_out.append(
            f"**Generated {pd.Timestamp(rendered_at, unit='s', tz='UTC').tz_convert(weeks.ET):%a %Y-%m-%d %H:%M ET} "
            f"-- kickoff ({lock['kickoff_label']}) has already passed. This report is history, not a decision.**"
        )
    else:
        lines_out.append(
            f"**Generated {pd.Timestamp(rendered_at, unit='s', tz='UTC').tz_convert(weeks.ET):%a %Y-%m-%d %H:%M ET} "
            f"-- {lock['minutes']} minutes to the first Sunday kickoff ({lock['kickoff_label']}). "
            "After kickoff this report is history.**"
        )
    lines_out.append("")

    lines_out.append("## Freshness")
    lines_out.extend(freshness_lines(freshness(season=season)))
    if gate["insufficient"]:
        lines_out.append(f"- odds (`pre_lock`): **{INSUFFICIENT_DATA}** -- {gate['reason']}")
    else:
        lines_out.append(
            f"- odds (`pre_lock`): {_fmt_et(gate['ran_at'])} -- verified, "
            f"{gate['credits_spent'] if gate['credits_spent'] is not None else INSUFFICIENT_DATA} credits spent"
        )
    lines_out.append(
        "_The `odds:` line above is `loaders._odds_freshness`, hardcoded to the `slate_context` job -- "
        "it reads Tuesday's capture. The `pre_lock` line is this report's own read._"
    )
    lines_out.append("")

    lines_out.append("## Decisions due")
    lines_out.append(f"**The final lock, before {lock['kickoff_label']}.**")
    lines_out.append("")
    if lock["minutes"] >= 0:
        lines_out.append(f"- {lock['minutes']} minutes remain (source: {lock['source']}).")
    else:
        lines_out.append(f"- Kickoff passed {abs(lock['minutes'])} minutes ago (source: {lock['source']}).")
    lines_out.append(f"- {len(undecided_rows)} slot(s) still undecided this morning.")
    if out_read["gate"]["insufficient"]:
        # Never "0 starters read OUT" off a read that did not run -- that is
        # an absence of evidence rendered as an all-clear.
        lines_out.append(f"- Starters reading `OUT`: {INSUFFICIENT_DATA} -- the overnight tier read could not run.")
    else:
        out_count = len(out_read["moved"]) + len(out_read["already_out"])
        lines_out.append(f"- {out_count} starter(s) read `OUT`.")
    lines_out.append("")

    lines_out.append("## Did `pre_lock` actually run")
    if gate["insufficient"]:
        lines_out.append(f"**{INSUFFICIENT_DATA}** -- {gate['reason']}")
    else:
        lines_out.append(f"- Ran at {_fmt_et(gate['ran_at'])}, `stale` flag clear.")
        lines_out.append(
            f"- Featured lines: newest capture {_fmt_captured_at(lines['current_at'])}"
            + (" -- from this run." if lines["from_this_run"] else
               " -- **not from this run**; the featured half returned nothing and Friday's lines are what render below.")
        )
        lines_out.append(
            f"- Props: newest capture {_fmt_captured_at(props['captured_at'])}"
            + (" -- from this run." if props["from_this_run"] else
               " -- **not from this run**; the props half of this morning's job returned nothing.")
        )
    lines_out.append("")

    lines_out.append("## Slots still undecided as of this morning")
    lines_out.append(
        "_Recomputed by `friday.recommended_lineup`/`friday.held_open_slots` against today's data. "
        "Nothing persists Friday's own list; these agree with it whenever no input has moved._"
    )
    lines_out.append("")
    if not undecided_rows:
        lines_out.append("No slot is undecided -- every recommended starter is either clear or clearly better.")
        lines_out.append("")
    for row in undecided_rows:
        lines_out.append(f"### {row['slot']} -- {row['incumbent_name']}")
        lines_out.append(f"{row['reason']}")
        lines_out.append("")
        lines_out.extend(table(
            ["player", "tier", "prop points", "markets", "ESPN projected", "ranked on"],
            [
                [c["player_name"], c["tier"] or INSUFFICIENT_DATA, num(c["prop_points"]),
                 c["markets"], num(c["projection"]), c["rank_source"]]
                for c in row["candidates"]
            ],
        ))
        lines_out.append("")
        if row["market_best"] and row["friday_best"]:
            if row["agrees"]:
                lines_out.append(f"Market and projection agree on {row['market_best']['player_name']}.")
            else:
                lines_out.append(
                    f"Market picks {row['market_best']['player_name']}; Friday's projection rule picks "
                    f"{row['friday_best']['player_name']}."
                )
        else:
            lines_out.append(f"No `pre_lock` quote for this slot -- ranked on ESPN's projection alone.")
        lines_out.append("")

    lines_out.append("## Starters whose tier reads `OUT`")
    if out_read["gate"]["insufficient"]:
        lines_out.append(f"{INSUFFICIENT_DATA} -- {out_read['gate']['reason']}")
    else:
        rows = out_read["moved"] + out_read["already_out"]
        lines_out.extend(table(
            ["player", "tier at baseline", "tier now", "since", "best bench replacement"],
            [
                [r["player_name"], r["tier_then"], r["tier_now"], r["since"],
                 r["replacement"]["player_name"] if r.get("replacement") else "no legal swap"]
                for r in rows
            ],
        ))
    lines_out.append("")

    lines_out.append("## The lineup you are locking")
    if recommended["insufficient"]:
        lines_out.append(f"{INSUFFICIENT_DATA} -- {recommended['reason']}")
    else:
        lines_out.extend(table(
            ["slot", "player", "projected", "tier", "prop points"],
            [
                [r["slot"], r["player_name"], num(r["projection"]),
                 (market_by_player.get(r["player_id"]) or {}).get("tier") or INSUFFICIENT_DATA,
                 num((market_by_player.get(r["player_id"]) or {}).get("points"))]
                for r in recommended["lineup"]
            ],
        ))
        if recommended["unfilled_slots"]:
            lines_out.append("")
            lines_out.append(f"Unfilled: {', '.join(recommended['unfilled_slots'])}.")
    lines_out.append("")

    lines_out.append("## Pre-lock featured lines")
    if lines["insufficient"]:
        lines_out.append(f"{INSUFFICIENT_DATA} -- {lines['reason']}")
    else:
        if not lines["from_this_run"]:
            lines_out.append(
                "**These rows are not from this morning's run** -- the newest capture predates it, so "
                "what follows is Friday's `line_movement` data."
            )
            lines_out.append("")
        lines_out.append(
            f"Capture {_fmt_captured_at(lines['current_at'])}"
            + (f", against {_fmt_captured_at(lines['prior_at'])}." if lines["prior_at"] else
               " -- no prior capture to diff against.")
        )
        lines_out.append("")
        lines_out.extend(table(
            ["team", "spread", "total", "implied", "implied prior", "d(implied)"],
            [
                [r["team"], num(r["spread"]), num(r["total"]), num(r["implied"]),
                 num(r["implied_prior"]), num(r["implied_delta"])]
                for r in lines["rows"]
            ],
        ))
    lines_out.append("")

    lines_out.append("## Pre-lock props -- still-undecided slots only")
    if props["insufficient"]:
        lines_out.append(f"{INSUFFICIENT_DATA} -- {props['reason']}")
    else:
        ranked = sorted(props["by_player"].values(), key=lambda v: -v["points"])
        lines_out.extend(table(
            ["player", "prop points", "markets"],
            [[v["player_name"], num(v["points"]), v["markets"]] for v in ranked],
        ))
        if props["unmatched_count"]:
            lines_out.append("")
            lines_out.append(
                f"_{props['unmatched_count']} prop row(s) matched no ESPN player and are excluded from "
                "the table above -- counted, never silently dropped._"
            )
    lines_out.append("")

    lines_out.append("## What this report cannot see")
    lines_out.extend(f"- {note}" for note in footer_notes)

    return "\n".join(lines_out) + "\n"


def payload(season, week, team_id, lock, gate, lines, props, market_by_player, undecided_rows,
            out_read, recommended, footer_notes, window=None, rendered_at=None):
    """The structured twin of `render`, over the identical argument list.

    Two things are specific to this report. The kickoff countdown renders
    *above* the freshness block rather than under a `##` heading, so it is a
    headingless leading section. And the freshness block carries a fifth
    `pre_lock` line that is this report's own read, kept apart from
    `loaders._odds_freshness`'s Tuesday `slate_context` capture. See
    espn_ff/report/payload.py.
    """
    rendered_at = rendered_at if rendered_at is not None else time.time()
    title = f"Final lock -- {season} week {week}"
    covers = f"week {week}'s Sunday lineup lock, before {lock['kickoff_label']}"
    generated = f"{pd.Timestamp(rendered_at, unit='s', tz='UTC').tz_convert(weeks.ET):%a %Y-%m-%d %H:%M ET}"

    header = payload_lib.header_block(title, week, covers, window, rendered_at)

    if lock["minutes"] < 0:
        countdown = (f"**Generated {generated} -- kickoff ({lock['kickoff_label']}) has already "
                     "passed. This report is history, not a decision.**")
    else:
        countdown = (f"**Generated {generated} -- {lock['minutes']} minutes to the first Sunday "
                     f"kickoff ({lock['kickoff_label']}). After kickoff this report is history.**")

    sections = [
        # No heading: the spec puts this above even the freshness block, so a
        # tab left open past kickoff says so at a glance.
        payload_lib.prose_section(
            "kickoff-countdown", None, [countdown], emphasis=True, level=None,
            data={
                "minutes_to_kickoff": lock["minutes"],
                "kickoff_passed": lock["minutes"] < 0,
                "kickoff_label": lock["kickoff_label"],
                "source": lock["source"],
            },
        ),
        _freshness_section(season, gate),
        _decisions_section(lock, undecided_rows, out_read),
        _pre_lock_ran_section(gate, lines, props),
        _undecided_section(undecided_rows),
        _out_section(out_read),
        _lineup_section(recommended, market_by_player),
        _featured_lines_section(lines),
        _props_section(props),
        payload_lib.list_section("cannot-see", "What this report cannot see", footer_notes),
    ]
    return header, sections


def _freshness_section(season, gate):
    if gate["insufficient"]:
        pre_lock = f"- odds (`pre_lock`): **{INSUFFICIENT_DATA}** -- {gate['reason']}"
    else:
        credits = gate["credits_spent"] if gate["credits_spent"] is not None else INSUFFICIENT_DATA
        pre_lock = f"- odds (`pre_lock`): {_fmt_et(gate['ran_at'])} -- verified, {credits} credits spent"
    return payload_lib.freshness_section(
        freshness(season=season),
        notes=[
            pre_lock,
            "_The `odds:` line above is `loaders._odds_freshness`, hardcoded to the `slate_context` "
            "job -- it reads Tuesday's capture. The `pre_lock` line is this report's own read._",
        ],
    )


def _decisions_section(lock, undecided_rows, out_read):
    body = [f"**The final lock, before {lock['kickoff_label']}.**"]
    if lock["minutes"] >= 0:
        body.append(f"- {lock['minutes']} minutes remain (source: {lock['source']}).")
    else:
        body.append(f"- Kickoff passed {abs(lock['minutes'])} minutes ago (source: {lock['source']}).")
    body.append(f"- {len(undecided_rows)} slot(s) still undecided this morning.")

    if out_read["gate"]["insufficient"]:
        body.append(f"- Starters reading `OUT`: {INSUFFICIENT_DATA} -- the overnight tier read could not run.")
        out_count = None
    else:
        out_count = len(out_read["moved"]) + len(out_read["already_out"])
        body.append(f"- {out_count} starter(s) read `OUT`.")

    return payload_lib.prose_section(
        "decisions-due", "Decisions due", body, emphasis=True,
        # `out_starter_count` is None, never 0, when the read did not run --
        # an absence of evidence must not serialize as an all-clear.
        data={
            "binding": True,
            "minutes_to_kickoff": lock["minutes"],
            "undecided_count": len(undecided_rows),
            "out_starter_count": out_count,
        },
    )


def _pre_lock_ran_section(gate, lines, props):
    heading = "Did `pre_lock` actually run"
    if gate["insufficient"]:
        return payload_lib.insufficient_section("pre-lock-ran", heading, gate["reason"])
    body = [
        f"- Ran at {_fmt_et(gate['ran_at'])}, `stale` flag clear.",
        f"- Featured lines: newest capture {_fmt_captured_at(lines['current_at'])}"
        + (" -- from this run." if lines["from_this_run"] else
           " -- **not from this run**; the featured half returned nothing and Friday's lines are "
           "what render below."),
        f"- Props: newest capture {_fmt_captured_at(props['captured_at'])}"
        + (" -- from this run." if props["from_this_run"] else
           " -- **not from this run**; the props half of this morning's job returned nothing."),
    ]
    return payload_lib.prose_section(
        "pre-lock-ran", heading, body,
        # Whether each half is this morning's data is the whole point of the
        # section, and in markdown it is a bolded clause mid-sentence.
        data={
            "ran_at": gate["ran_at"], "credits_spent": gate["credits_spent"],
            "lines_from_this_run": lines["from_this_run"],
            "props_from_this_run": props["from_this_run"],
        },
    )


def _undecided_section(undecided_rows):
    """`## Slots still undecided as of this morning`, one `###` per slot."""
    heading = "Slots still undecided as of this morning"
    note = (
        "_Recomputed by `friday.recommended_lineup`/`friday.held_open_slots` against today's data. "
        "Nothing persists Friday's own list; these agree with it whenever no input has moved._"
    )
    if not undecided_rows:
        return payload_lib.prose_section(
            "slots-undecided", heading,
            [note, "No slot is undecided -- every recommended starter is either clear or clearly better."],
            data={"count": 0},
        )

    columns = [
        payload_lib.column("player_name", "player", "string"),
        payload_lib.column("tier", "tier", "string"),
        payload_lib.column("prop_points", "prop points", "number"),
        payload_lib.column("markets", "markets", "integer"),
        payload_lib.column("projection", "ESPN projected", "number"),
        payload_lib.column("rank_source", "ranked on", "string"),
    ]
    blocks = []
    for row in undecided_rows:
        if row["market_best"] and row["friday_best"]:
            verdict = (
                f"Market and projection agree on {row['market_best']['player_name']}."
                if row["agrees"] else
                f"Market picks {row['market_best']['player_name']}; Friday's projection rule picks "
                f"{row['friday_best']['player_name']}."
            )
        else:
            verdict = "No `pre_lock` quote for this slot -- ranked on ESPN's projection alone."
        blocks.append(payload_lib.table_section(
            f"undecided-{row['slot'].lower().replace('/', '-')}",
            f"{row['slot']} -- {row['incumbent_name']}",
            columns, payload_lib.rows(row["candidates"], columns),
            notes=[row["reason"], verdict], level=3,
            data={
                "slot": row["slot"], "incumbent_name": row["incumbent_name"],
                "market_best": (row["market_best"] or {}).get("player_name"),
                "friday_best": (row["friday_best"] or {}).get("player_name"),
                # None, not False, when there is no market quote to agree
                # with -- "they disagree" and "only one of them spoke" are
                # different answers.
                "agrees": row["agrees"] if (row["market_best"] and row["friday_best"]) else None,
            },
        ))
    return payload_lib.blocks_section(
        "slots-undecided", heading, blocks, data={"count": len(undecided_rows)},
    )


def _out_section(out_read):
    heading = "Starters whose tier reads `OUT`"
    if out_read["gate"]["insufficient"]:
        return payload_lib.insufficient_section("starters-out", heading, out_read["gate"]["reason"])
    rows_in = out_read["moved"] + out_read["already_out"]
    columns = [
        payload_lib.column("player_name", "player", "string"),
        payload_lib.column("tier_then", "tier at baseline", "string"),
        payload_lib.column("tier_now", "tier now", "string"),
        payload_lib.column("since", "since", "string"),
        payload_lib.column("replacement_name", "best bench replacement", "string"),
        payload_lib.column("no_legal_swap", "no legal swap", "boolean"),
    ]
    rows = [
        {
            "player_name": payload_lib.unset(r["player_name"]),
            "tier_then": payload_lib.unset(r["tier_then"]),
            "tier_now": payload_lib.unset(r["tier_now"]),
            "since": payload_lib.unset(r["since"]),
            "replacement_name": (
                payload_lib.unset(r["replacement"]["player_name"]) if r.get("replacement") else None
            ),
            "no_legal_swap": not r.get("replacement"),
        }
        for r in rows_in
    ]
    return payload_lib.table_section(
        "starters-out", heading, columns, rows,
        data={"count": len(rows_in), "moved_count": len(out_read["moved"]),
              "already_out_count": len(out_read["already_out"])},
    )


def _lineup_section(recommended, market_by_player):
    heading = "The lineup you are locking"
    if recommended["insufficient"]:
        return payload_lib.insufficient_section("lineup", heading, recommended["reason"])
    columns = [
        payload_lib.column("slot", "slot", "string"),
        payload_lib.column("player_name", "player", "string"),
        payload_lib.column("projection", "projected", "number"),
        payload_lib.column("tier", "tier", "string"),
        payload_lib.column("prop_points", "prop points", "number"),
    ]
    rows = []
    for r in recommended["lineup"]:
        market = market_by_player.get(r["player_id"]) or {}
        rows.append({
            "slot": payload_lib.unset(r["slot"]),
            "player_name": payload_lib.unset(r["player_name"]),
            "projection": payload_lib.unset(r["projection"]),
            "tier": payload_lib.unset(market.get("tier")),
            "prop_points": payload_lib.unset(market.get("points")),
        })
    notes = []
    if recommended["unfilled_slots"]:
        notes.append(f"Unfilled: {', '.join(recommended['unfilled_slots'])}.")
    return payload_lib.table_section(
        "lineup", heading, columns, rows, notes=notes,
        data={"unfilled_slots": recommended["unfilled_slots"]},
    )


def _featured_lines_section(lines):
    heading = "Pre-lock featured lines"
    if lines["insufficient"]:
        return payload_lib.insufficient_section("featured-lines", heading, lines["reason"])
    notes = []
    if not lines["from_this_run"]:
        notes.append(
            "**These rows are not from this morning's run** -- the newest capture predates it, so "
            "what follows is Friday's `line_movement` data."
        )
    notes.append(
        f"Capture {_fmt_captured_at(lines['current_at'])}"
        + (f", against {_fmt_captured_at(lines['prior_at'])}." if lines["prior_at"] else
           " -- no prior capture to diff against.")
    )
    columns = [
        payload_lib.column("team", "team", "string"),
        payload_lib.column("spread", "spread", "number"),
        payload_lib.column("total", "total", "number"),
        payload_lib.column("implied", "implied", "number"),
        payload_lib.column("implied_prior", "implied prior", "number"),
        payload_lib.column("implied_delta", "d(implied)", "number"),
    ]
    return payload_lib.table_section(
        "featured-lines", heading, columns, payload_lib.rows(lines["rows"], columns), notes=notes,
        data={"from_this_run": lines["from_this_run"], "current_at": lines["current_at"],
              "prior_at": lines["prior_at"]},
    )


def _props_section(props):
    heading = "Pre-lock props -- still-undecided slots only"
    if props["insufficient"]:
        return payload_lib.insufficient_section("pre-lock-props", heading, props["reason"])
    columns = [
        payload_lib.column("player_name", "player", "string"),
        payload_lib.column("points", "prop points", "number"),
        payload_lib.column("markets", "markets", "integer"),
    ]
    ranked = sorted(props["by_player"].values(), key=lambda v: -v["points"])
    notes = []
    if props["unmatched_count"]:
        notes.append(
            f"_{props['unmatched_count']} prop row(s) matched no ESPN player and are excluded from "
            "the table above -- counted, never silently dropped._"
        )
    return payload_lib.table_section(
        "pre-lock-props", heading, columns, payload_lib.rows(ranked, columns), notes=notes,
        data={"unmatched_count": props["unmatched_count"], "from_this_run": props["from_this_run"],
              "captured_at": props["captured_at"]},
    )


def build(season, week, team_id=None):
    """Assemble the full Sunday report as markdown text. `week` is the
    current scoring period with no offset, same contract as
    wednesday.build/friday.build/saturday.build."""
    team_id = team_id if team_id is not None else config.TEAM_ID
    rendered_at = time.time()
    today = _et_date(rendered_at)

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

    signals_df = wednesday.practice_signals(week_rosters, today=today) if not week_rosters.empty else pd.DataFrame()
    trajectory_by_id = (
        dict(zip(signals_df["player_id"], signals_df["practice_trajectory"])) if not signals_df.empty else {}
    )

    gate = pre_lock_read(today=today)
    lines = featured_lines(week, gate)
    props = pre_lock_props(week, gate, pool_df)

    market_by_player = dict(props["by_player"])
    for pid, tier in avail_by_id.items():
        market_by_player.setdefault(pid, {})
        market_by_player[pid] = {**market_by_player[pid], "tier": tier}

    recommended, held_rows = undecided_slots(
        week_rosters, pool_df, roster_slots_df, allowed_slots, avail_by_id, trajectory_by_id
    )
    undecided_rows, no_market_names = ranked_undecided(
        held_rows, recommended, props["by_player"], avail_by_id
    ) if held_rows else ([], set())

    out_read = overnight_out(
        week_rosters, starters, bench, pool_df, allowed_slots, avail_by_id, today
    )

    lock = minutes_to_lock(season, week, rendered_at)

    footer_notes = list(FOOTER_NOTES)
    for name, (_, stale) in freshness(season=season).items():
        if stale:
            footer_notes.append(f"The {name} feed is stale as of this report's generation.")
    export_warning = espn_export_warning()
    if export_warning:
        footer_notes.append(f"The last ESPN export shrank -- {export_warning}.")
    if gate["insufficient"]:
        footer_notes.append(
            f"`pre_lock` could not be verified -- {gate['reason']}. Every market figure above is "
            "Friday's data or none."
        )
    else:
        if not lines["from_this_run"]:
            footer_notes.append(
                "The newest team_totals capture predates this morning's `pre_lock` run -- its "
                "featured-odds half returned nothing and Friday's `line_movement` lines are what "
                "rendered above."
            )
        if not props["from_this_run"]:
            footer_notes.append(
                "The newest player_props capture predates this morning's `pre_lock` run -- its props "
                "half returned nothing for this week."
            )
    if props["insufficient"]:
        footer_notes.append(f"The `pre_lock` props could not be read -- {props['reason']}.")
    elif props["unmatched_count"]:
        footer_notes.append(
            f"{props['unmatched_count']} prop row(s) matched no ESPN player -- counted, never dropped."
        )
    if out_read["gate"]["insufficient"]:
        footer_notes.append(
            f"The overnight tier read could not run -- {out_read['gate']['reason']}. No starter is "
            "shown as having moved to OUT above, which is an absence of evidence, not an all-clear."
        )
        footer_notes.append(
            "The tier and injury_status columns above come from the newest Sleeper slim snapshot on "
            "disk, whatever its date -- `availability.read` has no gate of its own."
        )
    if out_read["unmatched_names"]:
        footer_notes.append(
            "No Sleeper match on both the baseline and current snapshot -- counted, not read as "
            "unchanged -- for: " + ", ".join(sorted(out_read["unmatched_names"]))
        )
    if recommended["insufficient"]:
        footer_notes.append(f"The lineup solve could not run -- {recommended['reason']}.")
    else:
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
    if out_read["fallback_names"]:
        footer_notes.append(
            "Slot eligibility came from the position fallback (no player-pool row) for: "
            + ", ".join(sorted(out_read["fallback_names"]))
        )
    if out_read["nan_names"]:
        footer_notes.append(
            "Projections read NaN and could not be ranked for: " + ", ".join(sorted(out_read["nan_names"]))
        )
    if no_market_names:
        footer_notes.append(
            "Ranked on ESPN's `week_projected` rather than the `pre_lock` consensus -- no prop quote in "
            "this morning's capture -- for: " + ", ".join(sorted(no_market_names))
        )
    if lock["minutes"] < 0:
        footer_notes.append(
            "This report was generated after the first Sunday kickoff. It is history, not a decision."
        )
    if lock["source"] != "nflverse schedules":
        footer_notes.append(
            f"The first Sunday kickoff could not be read -- {lock['reason']}; the deadline above is the "
            "spec's default, not this week's slate."
        )
    divergence_df = tuesday.slot_map_divergence(pool_df, allowed_slots)
    if not divergence_df.empty:
        footer_notes.append(
            f"{len(divergence_df)} player(s) in this week's pool have eligible_slots that diverge from "
            "the position-fallback map -- the fallback path may be wrong for them specifically."
        )

    args = (
        season, week, team_id, lock, gate, lines, props, market_by_player, undecided_rows,
        out_read, recommended, footer_notes,
    )
    kwargs = {"window": weeks.week_window(season, week), "rendered_at": rendered_at}
    header, sections = payload(*args, **kwargs)
    return payload_lib.RenderedReport(
        render(*args, **kwargs), {"header": header, "sections": sections}
    )
