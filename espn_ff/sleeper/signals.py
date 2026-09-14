"""Derived Sleeper signals: availability tier, practice trajectory, depth
chart delta, trending flag.

The distinction this whole module exists to make land in the data: "Q + DNP
Friday" and "Q + Full Friday" are completely different situations, and ESPN
shows the same "Q" for both.

Trending is an alerting signal, not a ranking input -- carried as display
context only. It must never enter a score, sort or coefficient.
"""

import datetime as dt

import pandas as pd

# Doubtful/Out/IR/PUP/Suspension all collapse to OUT regardless of practice --
# none of them carry the practice-day nuance that Questionable does.
OUT_STATUSES = {"OUT", "IR", "PUP", "SUSPENSION", "SUS", "DOUBTFUL"}

PRACTICE_DNP = {"DNP"}
PRACTICE_LIMITED = {"LP", "LIMITED"}
PRACTICE_FULL = {"FP", "FULL"}

WEDNESDAY, THURSDAY, FRIDAY = 2, 3, 4


def availability_tier(injury_status, practice_participation):
    """One of OUT, HIGH_RISK, COIN_FLIP, LIKELY_PLAYS, UNKNOWN, CLEAR.

    UNKNOWN is a deliberate placeholder: a Questionable player with no
    practice_participation value on record cannot resolve to a
    practice-driven tier, and folding that case into COIN_FLIP would claim
    more certainty than the data supports. Whether it should collapse into
    COIN_FLIP instead is the one decision left open pending the
    practice_participation coverage numbers (scripts/practice_coverage.py).
    """
    status = "" if pd.isna(injury_status) else str(injury_status).strip().upper()
    practice = None if pd.isna(practice_participation) else str(practice_participation).strip().upper() or None

    if status in OUT_STATUSES:
        return "OUT"
    if status == "QUESTIONABLE":
        if practice in PRACTICE_DNP:
            return "HIGH_RISK"
        if practice in PRACTICE_LIMITED:
            return "COIN_FLIP"
        if practice in PRACTICE_FULL:
            return "LIKELY_PLAYS"
        return "UNKNOWN"
    return "CLEAR"


def _week_dates(today):
    monday = today - dt.timedelta(days=today.weekday())
    return {
        "wed": monday + dt.timedelta(days=WEDNESDAY),
        "thu": monday + dt.timedelta(days=THURSDAY),
        "fri": monday + dt.timedelta(days=FRIDAY),
    }


def practice_trajectory(snapshots_by_date, sleeper_id, today=None):
    """Wed/Thu/Fri practice_participation for the current NFL week.

    Sleeper only returns a single current value, not a history -- this reads
    the sequence out of our own daily snapshot store, keyed by date. A day we
    have no snapshot for (weekend runs, a missed job, day 1 of the job)
    reads as None; the caller renders that as "--" rather than hiding it.
    """
    today = today or dt.date.today()
    values = {}
    for label, day in _week_dates(today).items():
        df = snapshots_by_date.get(day)
        value = None
        if df is not None and not df.empty:
            match = df.loc[df["sleeper_id"] == sleeper_id, "practice_participation"]
            if len(match) and pd.notna(match.iloc[0]):
                value = str(match.iloc[0])
        values[label] = value
    return values["wed"], values["thu"], values["fri"]


def format_trajectory(trajectory):
    return " / ".join(v if v else "—" for v in trajectory)


def depth_chart_delta(current_row, snapshots_by_date, sleeper_id, lookback_days=3, today=None):
    """Diff depth_chart_order against the snapshot `lookback_days` prior.

    Falls back to the oldest available snapshot when nothing exists at
    exactly that distance, and reports the actual gap used (`days_used`)
    rather than silently comparing against a different interval. Returns
    None when there is no prior snapshot at all.

    `promoted` flags crossing into depth_chart_order 1 or 2 at the player's
    own depth_chart_position -- the case worth surfacing separately from a
    generic "moved up" improvement.
    """
    today = today or dt.date.today()
    target_day = today - dt.timedelta(days=lookback_days)

    available = sorted(d for d in snapshots_by_date if d <= today and snapshots_by_date[d] is not None)
    if not available:
        return None

    chosen_day = target_day if target_day in snapshots_by_date else min(available)
    prior_df = snapshots_by_date[chosen_day]
    prior_match = prior_df.loc[prior_df["sleeper_id"] == sleeper_id]
    if prior_match.empty:
        return None

    prior_order = prior_match.iloc[0].get("depth_chart_order")
    prior_position = prior_match.iloc[0].get("depth_chart_position")
    current_order = current_row.get("depth_chart_order")
    current_position = current_row.get("depth_chart_position")

    improved = bool(
        pd.notna(prior_order)
        and pd.notna(current_order)
        and current_position == prior_position
        and current_order < prior_order
    )
    promoted = bool(
        pd.notna(current_order)
        and current_order in (1, 2)
        and (pd.isna(prior_order) or prior_order > 2)
    )

    return {
        "prior_order": prior_order,
        "current_order": current_order,
        "improved": improved,
        "promoted": promoted,
        "days_used": (today - chosen_day).days,
    }


def trending_flag(trending_df, sleeper_id, kind="add", limit=25, floor=0):
    """(flag, raw_count). flag is True only when the player is both in the
    top-`limit` by count for `kind` AND that count clears `floor` -- both are
    parameters, neither implies the other.

    Display context only: this never feeds a score or a sort.
    """
    subset = trending_df[trending_df["kind"] == kind]
    row = subset[subset["player_id"] == sleeper_id]
    count = int(row["count"].iloc[0]) if not row.empty else 0
    if row.empty:
        return False, count

    top_ids = set(subset.sort_values("count", ascending=False).head(limit)["player_id"])
    flag = sleeper_id in top_ids and count >= floor
    return flag, count
