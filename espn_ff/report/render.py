"""Shared markdown-rendering helpers for every report/*.py module. No disk
access here -- keeps this module unit-testable with no fixtures.
"""

import pandas as pd

from ..weeks import ET, format_window

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
    """Epoch seconds -> ET wall-clock, labelled. `pd.Timestamp(ts,
    unit="s")` alone is naive UTC printed with no zone suffix -- a UTC
    freshness block directly under an ET dateline is a worse trap than the
    one this fixes."""
    if ts is None:
        return "never"
    return pd.Timestamp(ts, unit="s", tz="UTC").tz_convert(ET).strftime("%Y-%m-%d %H:%M:%S ET")


def _fmt_rendered_at(ts):
    return pd.Timestamp(ts, unit="s", tz="UTC").tz_convert(ET).strftime("%a %Y-%m-%d %H:%M ET")


def header_lines(title, week, covers, window, rendered_at):
    """The dateline every report opens with: an H1 title, the dates this
    render actually covers, the calendar week window it was rendered
    against (`espn_ff/weeks.week_window`), and when it was generated. See
    docs/report-weekly-schedule.md's "What every report contains".

    `window` is `(start_et, end_et)` or None -- when the calendar isn't
    available, the `**Week N**` line renders INSUFFICIENT_DATA rather than
    being omitted, since a missing line is indistinguishable from a report
    that had nothing to say."""
    week_text = format_window(*window) if window is not None else INSUFFICIENT_DATA
    return [
        f"# {title}",
        "",
        f"**Covers** {covers}",
        f"**Week {week}** {week_text}",
        f"**Rendered** {_fmt_rendered_at(rendered_at)}",
    ]


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
