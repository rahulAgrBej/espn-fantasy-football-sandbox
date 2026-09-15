"""Report engine: renders the markdown reports specified in
docs/report-weekly-schedule.md from data already collected on disk. No
network calls -- every report here reads yesterday's (or today's) exports
and derived state, never fetches anything itself.

Boundaries mirror espn_ff/sleeper/ and espn_ff/nflverse/: `loaders.py` owns
disk access shared by every report (the latest data/out/ export per
dataset, and per-feed freshness), and each `<day>.py` module assembles one
day's report from the pieces in this package.
"""
