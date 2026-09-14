#!/usr/bin/env python3
"""practice_participation coverage report -- the gate before any Sleeper
signal informs a start/sit call.

Reports non-null practice_participation coverage overall, restricted to
Questionable players, broken out by position and by our own roster, plus
day-over-day change once at least two daily `sleeper` runs exist. Prints the
numbers and stops -- it does not decide anything on its own.

    .venv/bin/python scripts/practice_coverage.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from espn_ff import config
from espn_ff.cache import ttl_for
from espn_ff.client import EspnClient
from espn_ff.extract import rosters
from espn_ff.sleeper import signals, snapshots


def _coverage_rate(df):
    if df.empty:
        return None
    return df["practice_participation"].notna().mean()


def _print_rate(label, df, indent="  "):
    rate = _coverage_rate(df)
    if rate is None:
        print(f"{indent}{label:<40} n/a (n=0)")
    else:
        print(f"{indent}{label:<40} {rate:6.1%}  (n={len(df)})")


def main():
    dates = snapshots.list_slim_dates()
    if not dates:
        print("No Sleeper snapshots yet -- run `python -m espn_ff sleeper` first.")
        return 1

    latest_day = dates[-1]
    latest = snapshots.read_slim(latest_day)
    print(f"Snapshot: {latest_day}  ({len(latest)} active players)\n")

    print("Coverage")
    print("-" * 60)
    _print_rate("overall", latest)

    questionable = latest[latest["injury_status"].astype(str).str.upper() == "QUESTIONABLE"]
    _print_rate("Questionable players only", questionable)

    print("\nBy position")
    print("-" * 60)
    for pos, group in latest.groupby("position"):
        _print_rate(str(pos), group)

    print("\nDay-over-day change")
    print("-" * 60)
    if len(dates) >= 2:
        prior_day = dates[-2]
        prior = snapshots.read_slim(prior_day)
        merged = latest.merge(prior, on="sleeper_id", suffixes=("_new", "_old"), how="inner")
        had_any_value = merged["practice_participation_new"].notna() | merged["practice_participation_old"].notna()
        changed = merged["practice_participation_new"] != merged["practice_participation_old"]
        denom = max(int(had_any_value.sum()), 1)
        rate = (changed & had_any_value).sum() / denom
        print(f"  {prior_day} -> {latest_day}: {rate:6.1%} of players with any recorded value changed")
    else:
        print("  Need a second daily `sleeper` run to compute this -- only one snapshot exists.")

    print("\nCoverage on our roster")
    print("-" * 60)
    try:
        espn_client = EspnClient()
        current = espn_client.current_scoring_period()
        roster_payload = espn_client.get_league(
            ["mRoster", "mTeam"], scoring_period=current, ttl=ttl_for(current, current)
        )
        roster_df = rosters.rosters_frame(roster_payload, season=espn_client.season, week=current)
        team_espn_ids = set(roster_df.loc[roster_df["team_id"] == config.TEAM_ID, "player_id"])

        if config.SLEEPER_ID_MAP.exists() and team_espn_ids:
            id_map = pd.read_csv(config.SLEEPER_ID_MAP)
            our_sleeper_ids = set(id_map.loc[id_map["espn_player_id"].isin(team_espn_ids), "sleeper_id"])
            ours = latest[latest["sleeper_id"].isin(our_sleeper_ids)]
            _print_rate(f"team {config.TEAM_ID}", ours)
        else:
            print(f"  {config.SLEEPER_ID_MAP} not found -- run `sleeper` first to build the id map.")
    except Exception as exc:  # noqa: BLE001 -- this section is best-effort, not the gate
        print(f"  Skipped (network/roster lookup failed): {exc}")

    print("\nSpot check -- confirm these tiers by hand against known outcomes")
    print("-" * 60)
    for _, row in questionable.head(5).iterrows():
        tier = signals.availability_tier(row["injury_status"], row["practice_participation"])
        practice = row["practice_participation"] if pd.notna(row["practice_participation"]) else "—"
        print(f"  {str(row['full_name']):<24} {str(row['team']):<4} practice={practice:<8} -> {tier}")
    if questionable.empty:
        print("  No Questionable players in this snapshot.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
