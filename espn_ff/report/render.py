"""Shared markdown-rendering helpers for every report/*.py module. No disk
access here -- keeps this module unit-testable with no fixtures.
"""

import pandas as pd

INSUFFICIENT_DATA = "insufficient data"


def table(headers, rows):
    """Markdown pipe table as a list of lines. `None`/NaN cells render as
    `--`. An empty `rows` returns a single "_(none)_" line rather than a
    headers-only table."""
    if not rows:
        return ["_(none)_"]
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        cells = ["--" if value is None or pd.isna(value) else str(value) for value in row]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def num(value, places=1):
    """Formatted number, or INSUFFICIENT_DATA when `value` is None/NaN --
    keeps "render the words, not a figure" in one place instead of
    scattered :.1f f-strings."""
    if value is None or pd.isna(value):
        return INSUFFICIENT_DATA
    return f"{value:.{places}f}"


def _fmt_ts(ts):
    if ts is None:
        return "never"
    return pd.Timestamp(ts, unit="s").strftime("%Y-%m-%d %H:%M:%S")


def freshness_lines(fresh):
    """Iterates a fixed feed order, tolerating a `fresh` dict that carries
    fewer keys than the full set -- an older caller passing a three-key
    dict still works."""
    lines = []
    for name in ("sleeper", "nflverse", "espn", "odds"):
        if name not in fresh:
            continue
        ts, stale = fresh[name]
        flag = " (STALE)" if stale else ""
        lines.append(f"- {name}: {_fmt_ts(ts)}{flag}")
    return lines
