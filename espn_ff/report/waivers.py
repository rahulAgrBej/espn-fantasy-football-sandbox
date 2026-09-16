"""Tuesday's second report -- waiver wire and opening market, spec'd at
11:00 ET (docs/report-weekly-schedule.md's "Tuesday -- waiver wire and
opening market" section). Three traps this module exists to avoid.

First: `player-pool.csv` carries `on_team_id = 0` on every row (see
report/pool.py's module docstring) -- it must never be read here either;
free-agent availability always comes from `pool.free_agents`'s anti-join.

Second: `is_pending = True` on a transaction means the waiver has not
settled -- a claim rendered here as "won" from a pending row may not
actually be. Every settlement row carries its own `is_pending` rather than
being filtered or summarized past it.

Third: `trending_add` (espn_ff/sleeper/signals.py:8) is display-only
context in the Sleeper layer it comes from, and stays that way here -- it
is carried on add-candidate rows and never enters a sort key or a gap.

Every non-obvious claim below is tagged Observed (measured from an on-disk
artifact), Documented (asserted by a vendor or a docstring in this repo),
or Inferred (deduced from how the code behaves, not published anywhere),
per CLAUDE.md's sourcing discipline.
"""

import time

import pandas as pd

from .. import config, weeks
from ..names import normalize_team
from ..odds import projections as odds_projections
from ..odds import store as odds_store
from . import monday, pool, tuesday
from .loaders import freshness, latest_export
from .render import INSUFFICIENT_DATA, freshness_lines, header_lines, num, table

_SETTLEMENT_TYPES = {"WAIVER", "FREEAGENT"}

# `status` is the only field that says whether a claim actually succeeded.
# Observed 2026-09-16, the first waiver run ever captured on disk: EXECUTED,
# PENDING, FAILED_INVALIDPLAYERSOURCE, FAILED_ROSTERLIMIT, CANCELED. A losing
# claim is still a row -- ESPN records every team's attempt on a player, not
# just the winner's -- so filtering on type/item_type alone reports a lost
# player as won. Only EXECUTED moved a player.
_EXECUTED_STATUS = "EXECUTED"
_PENDING_STATUS = "PENDING"
_FAILED_PREFIX = "FAILED"
_CANCELED_STATUS = "CANCELED"


def settlements(transactions_df, week):
    """Claim/settlement rows in the {week - 1, week} window, each labelled
    with its *own* scoring_period rather than assuming one. This league
    processes waivers Tuesday night into Wednesday (Documented -- league
    setting, per the league manager), so a claim resolved that night falls
    in the *new* week's Tue 03:00 ET -> Tue 03:00 ET boundary.

    Both halves of that are now **Observed** (2026-09-16 pull, this league's
    first waiver run ever captured): `WAIVER`-type rows do exist, and ESPN
    stamped the Tuesday-night-processed claims `scoring_period = 2` -- the
    new week's, as the boundary implied. The prior docstring's "Observed:
    214/214 ... zero WAIVER" was measured against a period-1 snapshot that
    the export has since overwritten (see docs known gaps: the transactions
    export is not cumulative), so it is retired rather than carried.

    DRAFT/ROSTER-LINEUP/TRADE_PROPOSAL rows are excluded from the window and
    the excluded count is reported, not silently dropped.

    `waiver_type_observed` is checked against the *whole* export, not just
    this window -- it is a standing fact about this league, not a
    week-scoped one. `faab_in_use` is True only when some included row's
    bid_amount is actually positive; a report reading "0 spent" would claim
    more than a rolling-priority league (Inferred from `waiver_rank` being
    a populated 1..N sequence -- see `waiver_order`) actually evidences.
    """
    columns = [
        "scoring_period", "acting_team", "player_name", "type", "item_type",
        "execution_type", "is_pending", "bid_amount", "proposed_date",
    ]
    if transactions_df.empty:
        return {
            "insufficient": True, "reason": "no transactions export on disk",
            "rows": [], "excluded_count": 0, "pending_count": 0,
            "faab_in_use": False, "waiver_type_observed": False,
        }

    waiver_type_observed = bool((transactions_df["type"] == "WAIVER").any())

    window = {w for w in (week - 1, week) if w >= 1}
    windowed = transactions_df[transactions_df["scoring_period"].isin(window)]
    included = windowed[windowed["type"].isin(_SETTLEMENT_TYPES)]
    excluded_count = len(windowed) - len(included)

    rows = [{col: r[col] for col in columns} for _, r in included.iterrows()]
    pending_count = int((included["is_pending"] == True).sum())  # noqa: E712
    faab_in_use = bool((included["bid_amount"] > 0).any()) if not included.empty else False

    return {
        "insufficient": False,
        "rows": rows,
        "excluded_count": excluded_count,
        "pending_count": pending_count,
        "faab_in_use": faab_in_use,
        "waiver_type_observed": waiver_type_observed,
    }


# The waiver-processing *night* (Tuesday into Wednesday) is Documented --
# league setting, per the league manager. The 03:00 ET clock time below is
# Inferred: no artifact in this repo evidences when ESPN actually runs the
# batch. The one Observed data point (2026-09-16) has the claims stamped
# 07:02:58 ET, comfortably after this boundary, so an early boundary is the
# safe direction -- it can only make the gate stricter, never let a
# pre-settlement read through.
WAIVER_RUN_ET_HOUR = 3
_WAIVER_RUN_WEEKDAY = 2  # Wednesday, per datetime.weekday()


def last_waiver_boundary(rendered_at):
    """The most recent Wednesday `WAIVER_RUN_ET_HOUR` ET at or before
    `rendered_at` (epoch seconds). Returned as a tz-aware ET datetime."""
    now = pd.Timestamp(rendered_at, unit="s", tz="UTC").tz_convert(weeks.ET)
    boundary = now.normalize() + pd.Timedelta(hours=WAIVER_RUN_ET_HOUR)
    # Walk back to the most recent Wednesday boundary at or before `now`.
    days_since_wed = (boundary.weekday() - _WAIVER_RUN_WEEKDAY) % 7
    boundary -= pd.Timedelta(days=days_since_wed)
    if boundary > now:
        boundary -= pd.Timedelta(days=7)
    return boundary


def waiver_read_is_settled(fetched_at, rendered_at):
    """Whether an ESPN `mTransactions2` payload fetched at `fetched_at` is a
    post-settlement read for the waiver run preceding `rendered_at`.

    A boundary comparison rather than an hours-old threshold: the question
    is not "is this data recent" but "was it fetched after the run it claims
    to report". A 20-hour-old payload passed the flat 24-hour
    `ESPN_STALE_HOURS` check while predating the waiver run entirely
    (Observed 2026-09-16) -- which is how the Wednesday report came to print
    three confident zeros and assert a pull that never happened.

    Returns (settled: bool, reason: str|None)."""
    boundary = last_waiver_boundary(rendered_at)
    if fetched_at is None:
        return False, "the ESPN transactions payload has never been fetched"
    fetched = pd.Timestamp(fetched_at, unit="s", tz="UTC").tz_convert(weeks.ET)
    if fetched < boundary:
        return False, (
            f"the ESPN transactions payload was fetched {fetched:%a %Y-%m-%d %H:%M ET}, "
            f"before this week's waiver run (boundary {boundary:%a %Y-%m-%d %H:%M ET}) -- "
            "it cannot show what that run settled"
        )
    return True, None


def waiver_outcomes(transactions_df, pool_df, free_agents_df, week, team_id,
                    settled_read=None, since=None):
    """Successful (`status == EXECUTED`) FREEAGENT/WAIVER ADD/DROP rows in
    the same `{week - 1, week}` window `settlements()` uses, split three
    ways: what we claimed, what another team claimed (so an alternate can be
    offered), and who is newly available since last week.

    `status` is load-bearing, not cosmetic. ESPN records *every* team's
    attempt on a player, so a contested claim yields several rows for one
    player -- one EXECUTED for the winner and a FAILED_* for each loser
    (Observed 2026-09-16: Devaughn Vele carried an EXECUTED add for team 8
    and a FAILED_INVALIDPLAYERSOURCE add for team 5 at the same timestamp).
    Filtering on type/item_type alone therefore reports a player we *lost*
    as claimed by us, and shows one player claimed by two teams at once.

    Every row's `player_name`/`position`/`pro_team` is resolved from
    `pool_df` by `player_id`, never from `transactions_df["player_name"]` --
    a DROP row's `player_name` reads NaN in this export (the join comes
    from `weekly-rosters.csv`, which no longer lists a just-dropped
    player). `acting_team_id` (int) is the only field ever compared against
    `team_id` -- never `acting_team`, a display string that can legitimately
    mismatch the numeric id.

    `free_agents_df` is received, not recomputed -- the caller already
    needs it for the alternates step, so this stays a pure function over
    fixtures, same contract as `add_candidates`.

    `settled_read` is `(bool, reason)` from `waiver_read_is_settled`, passed
    in rather than computed here so this stays disk-free. When it says the
    payload predates the waiver run, this returns the insufficient shape --
    zeros from a pre-settlement read are a misleading figure, not a
    finding."""
    empty = {
        "insufficient": True, "claimed_by_us": [], "claimed_by_others": [], "newly_available": [],
        "pending_count": 0, "failed_count": 0, "unknown_count": 0, "excluded_count": 0,
        "unresolved_ids": set(),
    }
    if settled_read is not None and not settled_read[0]:
        return {**empty, "reason": settled_read[1]}
    if transactions_df.empty:
        return {**empty, "reason": "no transactions export on disk"}

    window = {w for w in (week - 1, week) if w >= 1}
    windowed = transactions_df[transactions_df["scoring_period"].isin(window)]

    # `since` is the current league week's start (Tue 03:00 ET). The
    # scoring_period window alone stopped being enough once the transaction
    # store became cumulative: `{week - 1, week}` legitimately contains last
    # week's moves, and rendering a week-old free-agent add under "claimed
    # last night" misstates when it happened. The period window stays as the
    # cheap pre-filter and as insurance against the scoring_period stamping
    # (only one run has ever been Observed); this narrows it to the run the
    # section actually reports on. Anchoring to the league week boundary
    # rather than the run's clock time is deliberate -- it captures both a
    # Tuesday-evening submission and its Wednesday-morning processing without
    # depending on when ESPN runs the batch.
    if since is not None and "proposed_date" in windowed.columns:
        proposed = pd.to_datetime(windowed["proposed_date"], errors="coerce")
        if proposed.dt.tz is None:
            proposed = proposed.dt.tz_localize(
                weeks.ET, ambiguous=True, nonexistent="shift_forward"
            )
        windowed = windowed[proposed >= pd.Timestamp(since)]

    included = windowed[windowed["type"].isin(_SETTLEMENT_TYPES)]
    excluded_count = len(windowed) - len(included)

    # Four exhaustive buckets, so no row can vanish between them. A null
    # status must compare False rather than propagate: `pd.NA == "PENDING"`
    # is NA, and an NA mask would let the row escape every bucket -- exactly
    # the silent drop this partition exists to prevent. `unknown` is kept
    # apart from `failed` because "we could not read this row's outcome" and
    # "this claim lost" are different facts, and reporting the first as the
    # second is the misleading-figure case this repo forbids.
    status = included["status"].astype("string").str.upper().fillna("")
    pending_mask = ((included["is_pending"] == True) | (status == _PENDING_STATUS))  # noqa: E712
    pending_mask = pending_mask.fillna(False).astype(bool)
    executed_mask = ~pending_mask & (status == _EXECUTED_STATUS)
    failed_mask = ~pending_mask & ~executed_mask & (
        status.str.startswith(_FAILED_PREFIX) | (status == _CANCELED_STATUS)
    )
    unknown_mask = ~pending_mask & ~executed_mask & ~failed_mask

    pending_count = int(pending_mask.sum())
    failed_count = int(failed_mask.sum())
    unknown_count = int(unknown_mask.sum())
    settled = included[executed_mask]

    pool_by_id = {}
    if not pool_df.empty:
        pool_by_id = {r["player_id"]: r for _, r in pool_df.iterrows()}

    unresolved_ids = set()

    def _resolve(player_id):
        row = pool_by_id.get(player_id)
        if row is None:
            unresolved_ids.add(player_id)
            return {"player_name": f"(unresolved player {player_id})", "position": None, "pro_team": None}
        return {"player_name": row["player_name"], "position": row["position"], "pro_team": row["pro_team"]}

    adds = settled[settled["item_type"] == "ADD"]
    claimed_by_us, claimed_by_others = [], []
    for _, r in adds.iterrows():
        info = _resolve(r["player_id"])
        row = {
            "player_id": r["player_id"], **info,
            "acting_team": r["acting_team"], "acting_team_id": r["acting_team_id"],
            "transaction_id": r["transaction_id"], "bid_amount": r["bid_amount"],
            "scoring_period": r["scoring_period"], "proposed_date": r["proposed_date"],
        }
        if r["acting_team_id"] == team_id:
            claimed_by_us.append(row)
        else:
            claimed_by_others.append(row)

    free_agent_ids = set(free_agents_df["player_id"]) if not free_agents_df.empty else set()
    drops = settled[settled["item_type"] == "DROP"]
    newly_available, seen_ids = [], set()
    for _, r in drops.iterrows():
        player_id = r["player_id"]
        if player_id not in free_agent_ids or player_id in seen_ids:
            continue
        seen_ids.add(player_id)
        info = _resolve(player_id)
        newly_available.append({
            "player_id": player_id, **info,
            "dropped_by_team": r["acting_team"], "dropped_by_team_id": r["acting_team_id"],
            "scoring_period": r["scoring_period"], "proposed_date": r["proposed_date"],
        })

    claimed_by_us.sort(key=lambda r: r["player_name"])
    claimed_by_others.sort(key=lambda r: r["player_name"])
    newly_available.sort(key=lambda r: r["player_name"])

    return {
        "insufficient": False,
        "claimed_by_us": claimed_by_us,
        "claimed_by_others": claimed_by_others,
        "newly_available": newly_available,
        "pending_count": pending_count,
        "failed_count": failed_count,
        "unknown_count": unknown_count,
        "excluded_count": excluded_count,
        "unresolved_ids": unresolved_ids,
    }


def waiver_order(teams_df, team_id):
    """`teams.csv` sorted by `waiver_rank` ascending. Deliberately does not
    inherit `tuesday.standings()`'s pre-season 0-0 gate: that gate exists
    because a 0-0 record is meaningless, but `waiver_rank` in week 1 is
    reverse draft order (Observed: a clean 1..12 permutation against a
    pre-season teams.csv where every record reads 0-0-0) -- it is usable
    from the very first week and reusing the standings gate here would
    wrongly suppress a column that is never actually insufficient this
    early."""
    if teams_df.empty or "waiver_rank" not in teams_df.columns or teams_df["waiver_rank"].isna().all():
        return {"insufficient": True, "reason": "no usable waiver_rank column in the teams export"}

    sorted_df = teams_df.sort_values("waiver_rank", na_position="last").reset_index(drop=True)
    our_row = sorted_df[sorted_df["team_id"] == team_id]
    our_rank = our_row["waiver_rank"].iloc[0] if not our_row.empty else None
    return {
        "insufficient": False, "rows": sorted_df,
        "our_rank": our_rank, "our_rank_of": len(sorted_df),
    }


def team_totals(week):
    """Wraps `odds.projections.build(week=week)`'s team-totals frame into
    `{"insufficient", "reason", "by_team": {abbrev: implied_team_total},
    "captured_at"}`. Insufficient when the parquet is absent, the frame is
    empty for `week`, or `slate_context`'s own last-run entry is absent or
    stale -- the budget-abort case docs/odds-budget.md describes, where the
    prior snapshot survives looking identical to a fresh pull.

    `team` on the totals frame is already normalized to an NFL abbrev by
    odds/jobs.py:_flatten_featured via names.normalize_team, so it is used
    as-is. `event_id` never reaches `by_team` -- a bare 32-hex string in a
    committed report is rejected by .githooks/pre-commit, and Odds API
    event ids are indeed that shape -- 32 lowercase hex characters
    (Observed against a real 2026-09-15 slate response)."""
    empty = {"insufficient": True, "by_team": {}, "captured_at": None}

    if not config.ODDS_TEAM_TOTALS.exists():
        return {**empty, "reason": "no team_totals.parquet on disk"}

    last_run = odds_store.read_last_run("slate_context")
    if last_run is None:
        return {**empty, "reason": "no slate_context entry in last_run.json"}
    if last_run.get("stale"):
        return {**empty, "reason": f"slate_context's last run is marked stale: {last_run.get('reason')}"}

    _, team_totals_df = odds_projections.build(week=week)
    if team_totals_df.empty:
        return {**empty, "reason": f"no team_totals rows for week {week}"}

    raw = pd.read_parquet(config.ODDS_TEAM_TOTALS)
    captured_at = None
    if not raw.empty and "captured_at" in raw.columns:
        captured_at = pd.Timestamp(raw["captured_at"].max()).timestamp()

    by_team = dict(zip(team_totals_df["team"], team_totals_df["implied_team_total"]))
    return {"insufficient": False, "by_team": by_team, "captured_at": captured_at}


def week_projection(player_id, pool_df, roster_row=None):
    """Apples-to-apples projection lookup: `player-pool.csv`'s
    `week_projected` is the primary path and covers rostered players too
    (the pool is not ownership-filtered), so a bench player and a free
    agent are compared on the same column from the same export. Falls back
    to the roster row's own `projected` -- naming the source lets a caller
    surface a mixed comparison instead of hiding it."""
    if not pool_df.empty:
        row = pool_df[pool_df["player_id"] == player_id]
        if not row.empty and pd.notna(row["week_projected"].iloc[0]):
            return row["week_projected"].iloc[0], "pool"
    if roster_row is not None and pd.notna(roster_row.get("projected")):
        return roster_row.get("projected"), "roster"
    return None, None


def add_candidates(free_agents_df, rosters_df, week, team_id, pool_df, roster_slots_df, allowed_slots,
                    totals_by_team=None, per_slot=3):
    """One block per distinct starting slot (tuesday._slot_instances,
    de-duplicated), emitted most-constrained-first (fewest eligible
    free agents), reusing tuesday.optimal_lineup's ordering idea so the
    scarce slots lead.

    Blocks overlap on purpose and that is stated, not hidden: a WR is
    eligible at both WR and RB/WR, so those two blocks return the
    identical top-3 candidates. Each block is a self-contained "who is
    best available for this slot" answer, the same shape as
    tuesday.regret_table's independent counterfactuals, so rows are kept
    rather than de-duplicated.

    `bench_floor` is the weakest non-IR bench player eligible for that
    slot; None when no eligible bench player exists -- this is a live
    case, not hypothetical (Observed, week 2: our bench is 3 WR / 2 RB /
    1 QB, so TE/K/D-ST have no eligible bench floor at all). Those blocks
    instead carry `starter_context`: the current starter's own projection,
    shown as context beside an insufficient-data gap, never substituted
    for it.

    Returns (blocks, fallback_names, nan_names, mixed_source_names)."""
    totals_by_team = totals_by_team or {}

    week_rosters = pd.DataFrame()
    if not rosters_df.empty:
        week_rosters = rosters_df[(rosters_df["week"] == week) & (rosters_df["team_id"] == team_id)]
    bench = tuesday._bench(week_rosters) if not week_rosters.empty else week_rosters

    fallback_names, nan_names, mixed_source_names = set(), set(), set()

    fa_eligibility = {}
    for fa in free_agents_df.itertuples():
        slots, used_fallback = tuesday._eligible_slots(fa.player_id, fa.position, pool_df, allowed_slots)
        if used_fallback:
            fallback_names.add(fa.player_name)
        fa_eligibility[fa.player_id] = slots

    distinct_slots = list(dict.fromkeys(tuesday._slot_instances(roster_slots_df)))

    prelim = []
    for slot in distinct_slots:
        eligible_fa = free_agents_df[free_agents_df["player_id"].isin(
            {pid for pid, slots in fa_eligibility.items() if slot in slots}
        )]

        bench_scored = []
        for _, bp in bench.iterrows():
            bp_slots, bp_fallback = tuesday._eligible_slots(bp["player_id"], bp["position"], pool_df, allowed_slots)
            if bp_fallback:
                fallback_names.add(bp["player_name"])
            if slot not in bp_slots:
                continue
            value, source = week_projection(bp["player_id"], pool_df, bp)
            if source == "roster":
                mixed_source_names.add(bp["player_name"])
            if value is None or pd.isna(value):
                nan_names.add(bp["player_name"])
                continue
            bench_scored.append({"player_name": bp["player_name"], "projection": value, "source": source})

        bench_floor = min(bench_scored, key=lambda c: c["projection"]) if bench_scored else None

        starter_context = None
        if bench_floor is None and not week_rosters.empty:
            starters = week_rosters[(week_rosters["started"]) & (week_rosters["lineup_slot"] == slot)]
            if not starters.empty:
                srow = starters.iloc[0]
                value, source = week_projection(srow["player_id"], pool_df, srow)
                starter_context = {"player_name": srow["player_name"], "projection": value, "source": source}

        rows = []
        for fa in eligible_fa.itertuples():
            value, source = week_projection(fa.player_id, pool_df)
            if value is None or pd.isna(value):
                nan_names.add(fa.player_name)
                continue
            gap = (value - bench_floor["projection"]) if bench_floor else None
            rows.append({
                "player_name": fa.player_name, "position": fa.position, "pro_team": fa.pro_team,
                "week_projected": value, "gap": gap,
                "percent_owned": getattr(fa, "percent_owned", None),
                "implied_team_total": totals_by_team.get(normalize_team(fa.pro_team)),
                "trending_add": getattr(fa, "trending_add", None), "drop_name": None,
            })
        rows.sort(key=lambda r: r["week_projected"], reverse=True)
        rows = rows[:per_slot]

        prelim.append({
            "slot": slot, "bench_floor": bench_floor, "starter_context": starter_context,
            "rows": rows, "none_reason": None if rows else f"no eligible free agent found for {slot}",
            "_eligible_count": len(eligible_fa),
        })

    prelim.sort(key=lambda b: b["_eligible_count"])
    for block in prelim:
        del block["_eligible_count"]

    return prelim, fallback_names, nan_names, mixed_source_names


def pair_drops(blocks, drop_list):
    """Names a drop per add row from tuesday.drop_candidates's output
    (already excludes the sole backup at a one-deep position). Drops are
    distinct *within* a slot block -- add rows 1/2/3 at a slot get drops
    1/2/3, since adding two players at one slot is a real move -- and
    reused *across* blocks, since the arithmetic forces it: up to 7 slot
    blocks x 3 rows is up to 21 add rows against a legal drop pool that is
    typically single digits (Observed, week 2: 5). A globally-distinct
    assignment would render "no legal drop remains" on most rows, which is
    noise, not honesty; the mutual exclusivity is stated in the footer and
    the Decisions-due section instead of hidden."""
    if not drop_list:
        for block in blocks:
            for row in block["rows"]:
                row["drop_name"] = None
        return blocks
    for block in blocks:
        for i, row in enumerate(block["rows"]):
            row["drop_name"] = drop_list[i % len(drop_list)]["player_name"]
    return blocks


FOOTER_NOTES = [
    "This league processes waivers Tuesday night into Wednesday (Documented -- league setting, per "
    "the league manager). Claims submitted before tonight's run appear below as pending, not settled.",
    "`percent_owned` has no final state -- it moves continuously vendor-side (Inferred) -- so it is "
    "a rough ownership signal, not a settled one.",
    "`trending_add` is display-only context and never entered any score or sort here.",
    "The free-agent pool is `player-pool.csv` anti-joined against all twelve teams' rosters, so "
    "\"free agent\" means unowned **and** inside ESPN's default ~1,041-player pool -- a player "
    "outside that pool never appears.",
    "`is_pending = True` means waivers have not settled; a claim shown as won may not be.",
    "Which scoring period ESPN stamps on a Tuesday-processed claim is Inferred; both week - 1 and "
    "week are shown and labelled.",
    "Named drops repeat across slot blocks. Each row is roster-legal on its own, but the pairings "
    "are alternatives to one another beyond the stated legal-drop count -- they do not combine.",
    "Slot blocks share candidates: a WR is eligible at both WR and RB/WR, so the same player can "
    "appear in two blocks. Each block is a per-slot answer, not a distinct add.",
]


def render(
    season, week, team_id, settle, order, blocks, totals, drop_list, footer_notes,
    window=None, prev_window=None, rendered_at=None,
):
    """`window`/`prev_window` are `(start_et, end_et)` for `week`/`week -
    1`, from `espn_ff.weeks.week_window` -- passed in rather than looked
    up here so this stays a pure function over its fixtures, with no disk
    access."""
    rendered_at = rendered_at if rendered_at is not None else time.time()
    title = f"Waiver wire and opening market -- {season} week {week}"

    covers = f"week {week}'s waiver window"
    if prev_window:
        covers += f"; settlements also span week {week - 1} ({weeks.format_window(*prev_window)})"

    lines = header_lines(title, week, covers, window, rendered_at)
    lines.append("")

    lines.append("## Freshness")
    lines.extend(freshness_lines(freshness(season=season)))
    lines.append("")

    lines.append("## Decisions due")
    if order["insufficient"]:
        lines.append(f"Waiver order: **insufficient data** -- {order['reason']}")
    else:
        lines.append(f"Our waiver rank: {num(order['our_rank'], places=0)} of {order['our_rank_of']}.")
    total_rows = sum(len(b["rows"]) for b in blocks)
    clearing = sum(1 for b in blocks for r in b["rows"] if r["gap"] is not None and r["gap"] > 0)
    lines.append(f"{clearing} of {total_rows} add candidates below project to clear their slot's bench floor.")
    if drop_list:
        lines.append(
            f"{len(drop_list)} legal drop(s) exist; the pairings below are alternatives to one another "
            "beyond that count, not simultaneously-available moves."
        )
    lines.append(
        "_Claim-deadline guidance: this league processes waivers tonight (Tuesday into Wednesday) -- "
        "submit or adjust claims before then; results land in tomorrow morning's ESPN pull._"
    )
    lines.append("")

    lines.append("## Waiver settlements")
    if settle["insufficient"]:
        lines.append(f"**insufficient data** -- {settle['reason']}")
    else:
        if not settle["waiver_type_observed"]:
            lines.append(
                "_No `WAIVER`-type transaction has ever been observed in this league's export; "
                "rows below are `FREEAGENT` claims only._"
            )
        if not settle["faab_in_use"]:
            lines.append("_This league shows no FAAB bids in any observed transaction -- not \"0 spent\"._")
        if settle["pending_count"]:
            lines.append(f"_{settle['pending_count']} row(s) below are still pending -- not yet settled._")
        headers = ["period", "team", "player", "type", "item", "execution", "pending", "bid", "date"]
        rows = [
            [r["scoring_period"], r["acting_team"], r["player_name"], r["type"], r["item_type"],
             r["execution_type"], r["is_pending"], r["bid_amount"], r["proposed_date"]]
            for r in settle["rows"]
        ]
        lines.extend(table(headers, rows))
        if settle["excluded_count"]:
            lines.append("")
            lines.append(f"_{settle['excluded_count']} other transaction(s) in this window were DRAFT/"
                          "ROSTER-LINEUP/TRADE_PROPOSAL and are excluded above._")
    lines.append("")

    lines.append("## Waiver order")
    if order["insufficient"]:
        lines.append(f"**{INSUFFICIENT_DATA}** -- {order['reason']}")
    else:
        headers = ["rank", "team", "waiver_rank", "ours"]
        rows = [
            [i + 1, r["team_name"], r["waiver_rank"], "<--" if r["team_id"] == team_id else ""]
            for i, (_, r) in enumerate(order["rows"].iterrows())
        ]
        lines.extend(table(headers, rows))
    lines.append("")

    lines.append("## Opening market")
    if totals["insufficient"]:
        lines.append(f"**{INSUFFICIENT_DATA}** -- {totals['reason']}. `implied_team_total` tempering "
                      "was not applied to the add candidates below.")
    else:
        headers = ["team", "implied_team_total"]
        rows = [[team, num(value)] for team, value in sorted(totals["by_team"].items())]
        lines.extend(table(headers, rows))
    lines.append("")

    lines.append("## Add candidates")
    for block in blocks:
        lines.append(f"### {block['slot']}")
        if block["bench_floor"]:
            bf = block["bench_floor"]
            lines.append(f"_Bench floor: {bf['player_name']} ({num(bf['projection'])} proj, source={bf['source']})._")
        else:
            lines.append(f"_{INSUFFICIENT_DATA} -- no eligible bench player at this slot to set a floor._")
            if block["starter_context"]:
                sc = block["starter_context"]
                lines.append(f"_Current starter for context: {sc['player_name']} ({num(sc['projection'])} proj)._")
        if block["slot"] in ("WR", "RB/WR"):
            lines.append("_WR and RB/WR share eligible candidates -- this block may repeat the other's rows._")
        lines.append("")
        if block["rows"]:
            headers = ["player", "position", "pro_team", "week_projected", "gap", "percent_owned",
                       "implied_team_total", "trending_add", "drop"]
            rows = [
                [r["player_name"], r["position"], r["pro_team"], num(r["week_projected"]), num(r["gap"]),
                 num(r["percent_owned"]), num(r["implied_team_total"]), r["trending_add"], r["drop_name"]]
                for r in block["rows"]
            ]
            lines.extend(table(headers, rows))
        else:
            lines.append(f"_{block['none_reason']}_")
        lines.append("")

    lines.append("## Drop candidates")
    if drop_list:
        lines.append(
            f"{len(drop_list)} legal drop(s), ranked weakest ROS projection first -- named per add row above; "
            "reused across slot blocks, not simultaneously available."
        )
        headers = ["player", "position", "ROS projection (inferred)"]
        rows = [[d["player_name"], d["position"], num(d["ros_projection"])] for d in drop_list]
        lines.extend(table(headers, rows))
    else:
        lines.append("No bench player is a legal drop candidate this week.")
    lines.append("")

    lines.append("## What this report cannot see")
    lines.extend(f"- {n}" for n in footer_notes)
    return "\n".join(lines) + "\n"


def build(season, week, team_id=None):
    """Assemble the full waiver-wire report as markdown text. `week` is
    the current scoring period with **no offset** -- unlike
    tuesday.build's `review_week = week - 1`, the coming week *is* the
    current scoring period, and is the only week player-pool.csv carries
    (Observed: `week == 2` only in the 15-09-2026 export). The two Tuesday
    modules otherwise look interchangeable, so this contrast is stated
    explicitly rather than left implicit."""
    team_id = team_id if team_id is not None else config.TEAM_ID

    transactions_df = latest_export("transactions")
    teams_df = latest_export("teams")
    rosters_df = latest_export("weekly-rosters")
    pool_df = latest_export("player-pool")
    roster_slots_df = latest_export("roster-slots")
    allowed_slots = monday.league_slots(roster_slots_df)

    settle = settlements(transactions_df, week)
    order = waiver_order(teams_df, team_id)
    totals = team_totals(week)

    free_agents_df = pool.free_agents(week, pool_df=pool_df, rosters_df=rosters_df)
    blocks, fallback_names, nan_names, mixed_source_names = add_candidates(
        free_agents_df, rosters_df, week, team_id, pool_df, roster_slots_df, allowed_slots,
        totals_by_team=totals.get("by_team", {}),
    )
    drop_list = tuesday.drop_candidates(rosters_df, pool_df, week, team_id)
    blocks = pair_drops(blocks, drop_list)

    footer_notes = list(FOOTER_NOTES)
    for name, (_, stale) in freshness(season=season).items():
        if stale:
            footer_notes.append(f"The {name} feed is stale as of this report's generation.")
    if totals["insufficient"]:
        footer_notes.append(
            f"Opening-market tempering could not be applied to add candidates -- {totals['reason']}."
        )
    if fallback_names:
        footer_notes.append(
            "Slot eligibility came from the position fallback (no player-pool row) for: "
            + ", ".join(sorted(fallback_names))
        )
    if nan_names:
        footer_notes.append(
            "Projections read NaN -- excluded from add candidates -- for: " + ", ".join(sorted(nan_names))
        )
    if mixed_source_names:
        footer_notes.append(
            "Projections were compared across two exports (pool week_projected vs. roster projected) for: "
            + ", ".join(sorted(mixed_source_names))
        )
    if not settle["insufficient"] and not settle["waiver_type_observed"]:
        footer_notes.append("No WAIVER-type transaction has ever been observed in this league's export.")
    divergence_df = tuesday.slot_map_divergence(pool_df, allowed_slots)
    if not divergence_df.empty:
        footer_notes.append(
            f"{len(divergence_df)} player(s) in this week's pool have eligible_slots that diverge from "
            "the position-fallback map -- the fallback path may be wrong for them specifically."
        )

    return render(
        season, week, team_id, settle, order, blocks, totals, drop_list, footer_notes,
        window=weeks.week_window(season, week),
        prev_window=weeks.week_window(season, week - 1) if week - 1 >= 1 else None,
    )
