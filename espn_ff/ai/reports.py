"""Finding the reports that still need summarising, picking their priors,
and reading the header block back off a rendered report.

Ordering here is always by the **date encoded in the filename**, never by
mtime -- the same discipline `espn_ff/report/loaders.py:latest_export`
states and for the same reason. Both of this command's inputs arrive by
`aws s3 sync`, which stamps a fresh mtime on a file whose contents are
weeks old, so mtime here would be actively wrong rather than merely
unreliable.
"""

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# `2026-09-15-tuesday-waiver-wire.md` -> date, then `<day_label>-<slug>`.
FILENAME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-(.+)\.md$")

SEASON_DIR = re.compile(r"^\d{4}$")
WEEK_DIR = re.compile(r"^week-(\d+)$")

# The 5-line block `espn_ff/report/render.py:header_lines` emits. Parsed
# rather than recomputed so the envelope's `report` fields describe the
# artifact on disk, not a fresh render of it.
TITLE = re.compile(r"^# (.+)$")
COVERS = re.compile(r"^\*\*Covers\*\* (.*)$")
WEEK_LINE = re.compile(r"^\*\*Week (\d+)\*\* (.*)$")
RENDERED = re.compile(r"^\*\*Rendered\*\* (.*)$")

PRIOR_COUNT = 4


@dataclass(frozen=True)
class ReportFile:
    """One rendered report on disk, keyed the way the rest of this module
    sorts and matches on it."""

    path: Path
    season: int
    week: int
    rendered_on: date
    day_label: str
    slug: str
    day: str

    @property
    def stem(self):
        return self.path.stem

    @property
    def label(self):
        return f"{self.season} week {self.week}, rendered {self.rendered_on:%Y-%m-%d}"

    @property
    def sort_key(self):
        """Filename date first, week as a deterministic tie-break. Two
        same-type reports can only share a date by being re-renders of
        different weeks on one day (a backfill), which is rare enough to
        deserve a stable order rather than a special case."""
        return (self.rendered_on, self.week)


def report_index(reports=None):
    """`(day_label, slug) -> day key`, the reverse of `cli.REPORTS`.

    A rendered filename carries the day *label* and the slug, not the
    REPORTS key they came from -- "tuesday-waiver-wire.md" has to map back
    to "tuesday-waivers", which no amount of splitting on hyphens will tell
    you. Inverting the table is the only thing that does.

    `cli` is imported inside the function: `cli` imports this package, so a
    module-level import would be circular. Same idiom as
    `config.odds_api_key`'s local import of `OddsError`.
    """
    if reports is None:
        from ..cli import REPORTS as reports

    return {(label, slug): day for day, (_, label, slug) in reports.items()}


def scan(reports_dir, index=None):
    """Every report under `<reports_dir>/<season>/week-NN/*.md` that maps to
    a known report type, sorted oldest-first.

    A file that does not match -- a stray note, a report type that has since
    been renamed -- is skipped silently rather than guessed at. It has no
    day key, so there is no prompt to build for it.
    """
    index = report_index() if index is None else index
    reports_dir = Path(reports_dir)
    if not reports_dir.is_dir():
        return []

    found = []
    for season_dir in sorted(reports_dir.iterdir()):
        if not season_dir.is_dir() or not SEASON_DIR.match(season_dir.name):
            continue
        for week_dir in sorted(season_dir.iterdir()):
            week_match = WEEK_DIR.match(week_dir.name) if week_dir.is_dir() else None
            if not week_match:
                continue
            for path in sorted(week_dir.glob("*.md")):
                parsed = _parse_filename(path, int(season_dir.name), int(week_match.group(1)), index)
                if parsed is not None:
                    found.append(parsed)

    return sorted(found, key=lambda report: report.sort_key)


def _parse_filename(path, season, week, index):
    match = FILENAME.match(path.name)
    if not match:
        return None
    year, month, day_of_month, rest = match.groups()

    # Matched against the index rather than split on the first hyphen: the
    # slug carries hyphens of its own ("waiver-wire"), and a future day
    # label might too.
    for (day_label, slug), day in index.items():
        if rest == f"{day_label}-{slug}":
            return ReportFile(
                path=path,
                season=season,
                week=week,
                rendered_on=date(int(year), int(month), int(day_of_month)),
                day_label=day_label,
                slug=slug,
                day=day,
            )
    return None


def summary_path(summaries_dir, report):
    """Where a report's summary envelope lives, mirroring the report tree
    one prefix over: `<summaries>/<season>/week-NN/<same stem>.json`."""
    return Path(summaries_dir) / str(report.season) / f"week-{report.week:02d}" / f"{report.stem}.json"


def existing_envelope(summaries_dirs, report):
    """The parsed envelope for `report` from the first tree that has one.

    `summaries_dirs` is plural on purpose. A run consults two: the pulled
    cache of everything already on S3, and the output tree holding what this
    run has written so far. Checking only the first makes idempotency depend
    on the S3 round-trip having succeeded -- two runs before a push would
    summarise the same report twice. Checking both makes it a property of
    the command instead of a property of the bucket.

    This returns the *content* rather than a boolean, because since v2 an
    envelope can be complete (summary and news) or partial (summary written,
    news failed) -- so "does a file exist" is no longer enough to decide
    what a run owes.

    An unreadable envelope -- truncated by a killed run, hand-edited badly --
    is treated as absent. Regenerating costs one report; trusting a file we
    could not parse costs silence, and silence is this repo's documented
    failure mode.
    """
    if isinstance(summaries_dirs, (str, Path)):
        summaries_dirs = (summaries_dirs,)

    for directory in summaries_dirs:
        path = summary_path(directory, report)
        if not path.exists():
            continue
        try:
            envelope = json.loads(path.read_text())
        except (OSError, ValueError):
            print(f"  [warning] {path} could not be read as JSON -- treating it as missing")
            return None
        return envelope if isinstance(envelope, dict) else None
    return None


def envelope_needs(envelope, want_news=True):
    """What an envelope still owes: `"full"`, `"news"`, or None.

    Three states rather than two, which is what makes a failed news call
    recoverable. A report whose summary landed but whose grounded calls died
    owes only the news, and regenerating the summary to get it would re-bill
    ~27k prompt tokens for an answer already sitting on disk.

    `want_news=False` (the `--no-news` path) collapses this back to the
    original two states, so that flag can never make a run rewrite envelopes
    it has nothing new to put in.
    """
    if not isinstance(envelope, dict) or not envelope.get("summary_markdown"):
        return "full"
    if want_news and envelope.get("news") is None:
        return "news"
    return None


def pending_work(candidates, summaries_dirs, day=None, week=None, force=False, want_news=True):
    """`[(report, existing_envelope, mode)]` for every report still owing
    something, oldest-first. `mode` is `"full"` or `"news"`.

    The same filters and the same contract as `unsummarized`, which this
    supersedes for the run loop: input-free, idempotent and self-healing,
    now across three states instead of two. `--force` returns every
    candidate as `"full"` with its existing envelope still attached, so the
    caller can compare against what it is about to overwrite.
    """
    if isinstance(summaries_dirs, (str, Path)):
        summaries_dirs = (summaries_dirs,)

    if day is not None:
        candidates = [r for r in candidates if r.day == day]
    if week is not None:
        candidates = [r for r in candidates if r.week == week]

    work = []
    for report in candidates:
        envelope = existing_envelope(summaries_dirs, report)
        mode = "full" if force else envelope_needs(envelope, want_news=want_news)
        if mode is not None:
            work.append((report, envelope, mode))
    return work


def priors(all_reports, report, count=PRIOR_COUNT):
    """The `count` most recent same-type reports that precede `report`,
    oldest-first.

    Same season only. A cross-season prior would compare this week against a
    roster, a league and a scoring system that no longer exist, which reads
    as a change when it is a different league-year entirely.
    """
    earlier = [
        other
        for other in all_reports
        if other.day == report.day
        and other.season == report.season
        and other.path != report.path
        and other.sort_key < report.sort_key
    ]
    return sorted(earlier, key=lambda other: other.sort_key)[-count:]


def parse_header(text):
    """Read back the 5-line block `render.header_lines` emits.

    Returns {"title", "covers", "week", "week_window", "rendered"} with None
    for any field absent. Deliberately tolerant: a report whose header
    drifted still gets summarised, it just carries less provenance in its
    envelope. Only the first 20 lines are scanned, so a `**Rendered**`-shaped
    string deeper in the body cannot be mistaken for the header.
    """
    header = {"title": None, "covers": None, "week": None, "week_window": None, "rendered": None}

    for line in text.splitlines()[:20]:
        if header["title"] is None and (match := TITLE.match(line)):
            header["title"] = match.group(1).strip()
        elif match := COVERS.match(line):
            header["covers"] = match.group(1).strip()
        elif match := WEEK_LINE.match(line):
            header["week"] = int(match.group(1))
            header["week_window"] = match.group(2).strip()
        elif match := RENDERED.match(line):
            header["rendered"] = match.group(1).strip()

    return header
