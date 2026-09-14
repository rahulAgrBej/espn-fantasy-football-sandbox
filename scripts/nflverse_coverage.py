#!/usr/bin/env python3
"""nflverse coverage report -- the gate before any nflverse role signal
informs a start/sit call, in the same spirit as scripts/practice_coverage.py.

Reports pfr_id -> gsis_id resolution rate (with unresolved rows named), the
share of snap rows that found a stats_player row, ESPN roster coverage
(excluding D/ST), manifest freshness per dataset, and a spot-check of the
top-5 offense_pct players on the roster. Prints the numbers and stops -- it
does not decide anything on its own.

    .venv/bin/python scripts/nflverse_coverage.py --season 2026
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from espn_ff import config
from espn_ff.cache import ttl_for
from espn_ff.client import EspnClient
from espn_ff.extract import rosters
from espn_ff.nflverse import features, ids, store


def _print_rate(label, numerator, denominator, indent="  "):
    if denominator == 0:
        print(f"{indent}{label:<40} n/a (n=0)")
    else:
        print(f"{indent}{label:<40} {numerator / denominator:6.1%}  (n={denominator})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=config.SEASON)
    args = ap.parse_args()
    season = args.season

    if not store.local_path("snap_counts", season).exists():
        print(f"No nflverse snap_counts for {season} on disk -- run `python -m espn_ff nflverse` first.")
        return 1

    snaps = store.load("snap_counts", season=season)
    reg_snaps = snaps[(snaps["game_type"] == "REG") & (snaps["position"].isin(features.SKILL_POSITIONS))]
    stats = store.load("stats_player", season=season)

    xwalk = (
        pd.read_csv(config.NFLVERSE_XWALK, dtype={"gsis_id": str, "pfr_id": str})
        if config.NFLVERSE_XWALK.exists()
        else pd.DataFrame(columns=["gsis_id", "pfr_id", "espn_player_id"])
    )

    print(f"nflverse coverage -- season {season}\n")

    print("pfr_id -> gsis_id resolution")
    print("-" * 60)
    xwalk_by_pfr = xwalk.rename(columns={"pfr_id": "pfr_player_id"})
    orphans = ids.orphans(reg_snaps, xwalk_by_pfr, on="pfr_player_id", label_cols=["player", "team", "position", "week"])
    resolved = len(reg_snaps) - len(orphans)
    _print_rate("skill-position snap rows resolved", resolved, len(reg_snaps))
    if not orphans.empty:
        print(f"  unresolved: {orphans[['player', 'team', 'position', 'week']].to_string(index=False)}")

    print("\nsnap_counts -> stats_player coverage")
    print("-" * 60)
    resolved_snaps = reg_snaps.merge(xwalk[["pfr_id", "gsis_id"]], left_on="pfr_player_id", right_on="pfr_id", how="left")
    resolved_snaps = resolved_snaps.dropna(subset=["gsis_id"])
    has_stats = resolved_snaps.merge(
        stats[["player_id", "season", "week", "season_type"]],
        left_on=["gsis_id", "week"], right_on=["player_id", "week"], how="left",
    )
    found = has_stats["player_id"].notna().sum()
    _print_rate("snap rows with a stats_player row", found, len(resolved_snaps))
    print("  a drop here means the week is still landing, not that usage collapsed")

    print("\nESPN roster coverage (excluding D/ST)")
    print("-" * 60)
    try:
        espn_client = EspnClient()
        current = espn_client.current_scoring_period()
        roster_payload = espn_client.get_league(
            ["mRoster", "mTeam"], scoring_period=current, ttl=ttl_for(current, current)
        )
        roster_df = rosters.rosters_frame(roster_payload, season=espn_client.season, week=current)
        team_roster = roster_df[
            (roster_df["team_id"] == config.TEAM_ID) & (roster_df["position"] != "D/ST")
        ]
        matched_ids = set(xwalk["espn_player_id"].dropna().astype(int)) if not xwalk.empty else set()
        matched = team_roster["player_id"].isin(matched_ids).sum()
        _print_rate(f"team {config.TEAM_ID} players with a gsis_id match", matched, len(team_roster))
    except Exception as exc:  # noqa: BLE001 -- this section is best-effort, not the gate
        print(f"  Skipped (network/roster lookup failed): {exc}")

    print("\nManifest freshness")
    print("-" * 60)
    manifest = store.read_manifest()
    if not manifest:
        print("  No manifest yet -- run `nflverse` first.")
    for key, entry in sorted(manifest.items()):
        flag = "STALE" if store.is_stale(entry) else "ok"
        print(f"  {key:<22} {entry.get('status', '?'):<10} {entry.get('last_updated', '?'):<28} [{flag}]")

    print("\nSpot check -- top-5 offense_pct on our roster")
    print("-" * 60)
    try:
        feats = features.build([season])
        our_feats = feats[feats["espn_player_id"].isin(team_roster["player_id"])]
        latest_week = our_feats["week"].max()
        top5 = our_feats[our_feats["week"] == latest_week].sort_values("offense_pct", ascending=False).head(5)
        for _, row in top5.iterrows():
            print(
                f"  {str(row['player_name']):<24} {str(row['team']):<4} "
                f"offense_pct={row['offense_pct']:.2f}  target_share={row['target_share']:.2f}"
            )
    except Exception as exc:  # noqa: BLE001 -- best-effort, same as above
        print(f"  Skipped: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
