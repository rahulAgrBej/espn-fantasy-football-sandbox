"""Shared JSON-building helpers for every report/*.py module -- the twin of
render.py, emitting the same report as structured data instead of markdown.
No disk access here either, so this stays unit-testable with no fixtures.

Each `<day>.py` grows a `payload(...)` beside its `render(...)`, taking the
**identical** argument list, and `build()` calls both on one set of computed
values. That is what keeps the two artifacts from disagreeing: they cannot
differ on their inputs, only on how each presents them.

Two rules the helpers here exist to enforce:

**Every value passes through `clean`.** The report package is pandas all the
way down, and `numpy.float64`/`pandas.NaT`/`pandas.NA` are not JSON
serializable. A leak surfaces as a `TypeError` at `json.dumps`, which in a
scheduled run is the very last step -- after the markdown has already been
written and committed. `clean` is to JSON what render.table's `--` is to
markdown.

**Raw values and nulls, never display strings.** `render.num` renders None as
the literal "insufficient data" and `render.table` renders it as `--`; a
consumer of this payload needs `null` so it can decide for itself. `unset`
is `clean` plus that collapse, and it is what every table cell and every
`data` scalar goes through -- see `PLACEHOLDERS` for the three vocabularies
involved. Prose bodies and notes keep the strings verbatim, since there the
words are the report's own phrasing.

The one deliberate exception is the header and freshness blocks, which carry
the epoch *and* the ET string the markdown showed -- `render._fmt_ts`'s
formatting is genuinely lossy, and a reader comparing the two artifacts
should not have to reconstruct it.

The `{"insufficient": True, "reason": ...}` sentinel that monday.live_margin,
waivers.waiver_order, thursday.canonical_read, friday.line_movement_read and
sunday.pre_lock_read all return maps onto `insufficient_section`. That is how
docs/report-weekly-schedule.md's binding rule -- a stale or missing input
renders the words, not a number -- survives into JSON rather than collapsing
to a `null` a client would read as zero.
"""

import hashlib
import math
from dataclasses import dataclass

import pandas as pd

from ..weeks import ET, format_window

# 1 is the initial schema. Follow espn_ff/ai/summarize.py's convention when
# this changes: additive only, every existing key keeping its name, position
# and meaning, so a reader written against v1 keeps working.
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RenderedReport:
    """What every `build()` returns: the markdown artifact and the structured
    one, produced from the same values in the same call.

    A frozen dataclass rather than a bare tuple so `result.markdown` reads as
    itself at the call site in cli.cmd_report -- the same reason
    espn_ff/ai/reports.py:ReportFile is one.
    """

    markdown: str
    data: dict


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def clean(value):
    """One cell, as something `json.dumps` accepts.

    NaN/NaT/NA/None all collapse to None -- the report package uses them
    interchangeably for "no reading", and preserving the distinction would
    export an implementation detail of whichever feed produced the column.

    numpy scalars are unwrapped via `.item()`: `numpy.int64` is not an `int`
    as far as the json module is concerned, and a single one anywhere in a
    frame fails the whole write.
    """
    if value is None:
        return None
    if isinstance(value, (str, bool)):
        return value
    if isinstance(value, float) and math.isnan(value):
        return None

    # Containers recurse rather than falling through to `str(value)`. Several
    # sections carry a genuine list -- friday's fired swap rules and unfilled
    # slots, the streams a drop funds -- and stringifying one produces a
    # plausible-looking "['a', 'b']" that no consumer can parse back.
    if isinstance(value, (list, tuple, set)):
        return [clean(item) for item in value]
    if isinstance(value, dict):
        return {str(key): clean(item) for key, item in value.items()}
    if value is pd.NaT or value is pd.NA:
        return None
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if hasattr(value, "item"):
        # numpy scalar -- including numpy.bool_, which is not a bool.
        unwrapped = value.item()
        return None if isinstance(unwrapped, float) and math.isnan(unwrapped) else unwrapped
    if isinstance(value, (int, float)):
        return value

    # pd.isna raises on list-likes, so it is the last check rather than the
    # first: anything reaching here is a scalar of an unexpected type.
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


# Strings that mean "no reading" by the time a value reaches a table cell.
# Three separate vocabularies converge here: `render.num`/`availability.read`
# emit "insufficient data", `render.table` emits "--", and
# `sleeper_signals.format_trajectory` emits an em dash per missing day (see
# friday._EMPTY_TRAJECTORY, which names the all-missing form).
#
# In markdown those strings *are* the content. In JSON they must be null --
# a consumer handed "insufficient data" in a tier column either renders the
# words as a tier or tries to parse them as one. None of these is ever a
# legitimate value: there is no player named "--", and a trajectory of three
# em dashes means no signal on any of the three days.
PLACEHOLDERS = frozenset({"--", "—", "insufficient data", "— / — / —", "-- / -- / --"})


def unset(value):
    """`clean(value)`, with the placeholders above collapsed to None.

    Applied to every table cell and every `data` scalar, rather than at the
    handful of columns known to produce one today: the placeholder set is
    distinctive enough to be safe globally, and a per-column opt-in is a rule
    someone has to remember at each of eight modules' call sites.

    Deliberately *not* folded into `clean`. Prose bodies and section notes
    keep these strings verbatim -- there the words are the report's own
    phrasing, and stripping them would leave an empty sentence.
    """
    cleaned = clean(value)
    if isinstance(cleaned, str) and cleaned.strip() in PLACEHOLDERS:
        return None
    return cleaned


def column(key, label, type_):
    """One column descriptor. `type_` is the *semantic* type a consumer should
    expect -- "string", "number", "integer", "boolean" or "date" -- declared
    explicitly rather than sniffed from the frame's dtype, which is empty-frame
    dependent and would flip between runs."""
    return {"key": key, "label": label, "type": type_}


def _section(id, heading, level, kind, data=None, **fields):
    """Every section shares this spine: a stable `id`, the `heading` text the
    markdown printed, its `level`, and its `kind`.

    `heading` is None for a block that had no heading of its own -- the second
    table under Wednesday's "Watchlist", say. `level` is None with it, since a
    block with no heading occupies no heading level.

    `data` carries section-level scalars the markdown only ever states in
    prose: Wednesday's waiver-outcome counts, Tuesday's points-left-on-the-
    table. Without it those numbers exist in the payload solely inside an
    English sentence, which is precisely the thing this artifact exists to
    stop.
    """
    section = {"id": id, "heading": heading, "level": level, "kind": kind}
    section.update(fields)
    if data is not None:
        section["data"] = {key: unset(value) for key, value in data.items()}
    return section


def rows(frame, columns):
    """`frame` as a list of row dicts keyed by each column's `key`.

    `columns` is the same list handed to `table_section`, so the payload
    cannot carry a column it did not describe, nor describe one it does not
    carry. A key absent from the frame yields None rather than raising -- the
    same tolerance render.table shows when it prints `--`.
    """
    if frame is None or (hasattr(frame, "empty") and frame.empty):
        return []
    records = frame.to_dict("records") if hasattr(frame, "to_dict") else list(frame)
    return [{col["key"]: unset(record.get(col["key"])) for col in columns} for record in records]


def table_section(id, heading=None, columns=(), rows=(), notes=(), level=2, data=None):
    """A `kind: "table"` section. `notes` carries the italic caveat lines the
    markdown prints under a table -- they qualify the rows, so they travel
    with them rather than becoming a separate prose section."""
    return _section(
        id, heading, level, "table", data=data,
        columns=list(columns), rows=list(rows), notes=[str(note) for note in notes],
    )


def prose_section(id, heading=None, body=(), emphasis=False, level=2, data=None):
    """A `kind: "prose"` section. `body` is the list of lines the markdown
    emitted, markup and all -- a consumer that wants plain text can strip it,
    but one that wants the report's own emphasis cannot recover it if this
    strips it first.

    `emphasis` flags a section the markdown itself bolds (the "Decisions due"
    lead line), so a front end can style it without pattern-matching on `**`.
    """
    return _section(
        id, heading, level, "prose", data=data,
        emphasis=bool(emphasis), body=[str(line) for line in body],
    )


def list_section(id, heading=None, items=(), level=2, data=None):
    """A `kind: "list"` section -- the bulleted blocks, of which the closing
    "What this report cannot see" is in all eight reports."""
    return _section(id, heading, level, "list", data=data, items=[str(item) for item in items])


def blocks_section(id, heading=None, blocks=(), level=2, data=None):
    """A `kind: "blocks"` section: an ordered list of child sections under one
    heading. The single composite kind, and it is recursive -- a block is an
    ordinary section built by the helpers here, and may itself be a
    `blocks_section`.

    Two distinct markdown shapes collapse onto it, which is why there is one
    composite kind rather than two:

      * `###`/`####` nesting -- monday's Alternatives, waivers' Add
        candidates, wednesday's Waiver outcomes, sunday's undecided slots.
        The child carries its own `heading` and `level`.
      * several blocks under one `##` with no subheadings -- wednesday's
        Watchlist prints a table, an italic note, then a second table. The
        children carry `heading: None`.
    """
    return _section(id, heading, level, "blocks", data=data, blocks=list(blocks))


def insufficient_section(id, heading=None, reason="", level=2, data=None):
    """A `kind: "insufficient"` section -- the JSON form of the
    `{"insufficient": True, "reason": ...}` sentinel the day modules return.

    Deliberately not an empty table or a null: "we could not compute this, and
    here is why" and "we computed this and it was empty" are different facts,
    and docs/report-weekly-schedule.md requires the difference stay visible.
    """
    return _section(id, heading, level, "insufficient", data=data, reason=str(reason))


def _fmt_ts(ts):
    """The same ET rendering render._fmt_ts does, kept in step with it so the
    `*_display` fields here match the markdown exactly."""
    if ts is None:
        return "never"
    return pd.Timestamp(ts, unit="s", tz="UTC").tz_convert(ET).strftime("%Y-%m-%d %H:%M:%S ET")


def header_block(title, week, covers, window, rendered_at):
    """The structured twin of render.header_lines' five-line dateline.

    `week_window` carries ISO endpoints for a consumer that wants to compute
    with them, plus the `display` string the markdown printed. When the
    calendar is unavailable `window` is None and both endpoints are null while
    `display` keeps render.header_lines' "insufficient data" -- absent and
    unavailable stay distinguishable, per the rule in
    docs/report-weekly-schedule.md.
    """
    start, end = (window if window is not None else (None, None))
    return {
        "title": title,
        "covers": covers,
        "week": week,
        "week_window": {
            "start": start.isoformat() if start is not None else None,
            "end": end.isoformat() if end is not None else None,
            "display": format_window(start, end) if window is not None else "insufficient data",
        },
        "rendered_at": clean(rendered_at),
        "rendered_display": pd.Timestamp(rendered_at, unit="s", tz="UTC")
        .tz_convert(ET)
        .strftime("%a %Y-%m-%d %H:%M ET"),
    }


def freshness_block(fresh):
    """The structured twin of render.freshness_lines, in the same fixed feed
    order and equally tolerant of a `fresh` dict carrying fewer keys (saturday
    drops `odds`)."""
    feeds = []
    for name in ("sleeper", "nflverse", "espn", "odds"):
        if name not in fresh:
            continue
        ts, stale = fresh[name]
        feeds.append({
            "feed": name,
            "at": clean(ts),
            "at_display": _fmt_ts(ts),
            "stale": bool(stale),
        })
    return feeds


def freshness_section(fresh, id="freshness", heading="Freshness", level=2, notes=()):
    """`notes` exists for sunday, which prints an extra `- odds (pre_lock)`
    line under the standard four feeds."""
    return _section(
        id, heading, level, "freshness",
        feeds=freshness_block(fresh), notes=[str(note) for note in notes],
    )


def envelope(season, week, day, day_label, slug, stem, generated_at, header, sections, markdown):
    """The stored artifact, written to
    `reports-json/<season>/week-NN/<stem>.json`.

    `markdown` is embedded verbatim: the JSON is a strict superset of the
    file next to it, so no restructuring here can lose information, and
    `markdown_sha256` lets a reader confirm the embedded copy is the same
    bytes as the `.md` in the bucket's `reports/` prefix -- the same integrity
    contract espn_ff/ai/summarize.py's `report.sha256` provides for summaries.

    `related.summary_path` is **derived, not observed**. This artifact is
    written before any summary exists, so that key names where the envelope
    will live if and when `summarize` produces one; it is not a claim that the
    file is there. See docs/report-json.md.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "season": season,
        "week": week,
        "day": day,
        "day_label": day_label,
        "slug": slug,
        "stem": stem,
        "generated_at": generated_at,
        "header": header,
        "sections": list(sections),
        "markdown": markdown,
        "markdown_sha256": sha256(markdown),
        "related": {
            "markdown_path": f"reports/{season}/week-{week:02d}/{stem}.md",
            "summary_path": f"summaries/{season}/week-{week:02d}/{stem}.json",
        },
    }
