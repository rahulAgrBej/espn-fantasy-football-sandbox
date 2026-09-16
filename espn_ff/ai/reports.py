"""Finding the reports that still need summarising, picking their priors,
and reading the header block back off a rendered report.

Ordering here is always by the **date encoded in the filename**, never by
mtime -- the same discipline `espn_ff/report/loaders.py:latest_export`
states and for the same reason. Both of this command's inputs arrive by
`aws s3 sync`, which stamps a fresh mtime on a file whose contents are
weeks old, so mtime here would be actively wrong rather than merely
unreliable.
"""

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


def has_summary(summaries_dirs, report):
    """True if a summary for `report` exists in ANY of the given trees.

    Plural on purpose. A run consults two: the pulled cache of everything
    already on S3, and the output tree holding what this run has written so
    far. Checking only the first makes idempotency depend on the S3
    round-trip having succeeded -- two runs before a push would summarise
    the same report twice. Checking both makes it a property of the command
    instead of a property of the bucket.
    """
    return any(summary_path(directory, report).exists() for directory in summaries_dirs)


def unsummarized(candidates, summaries_dirs, day=None, week=None, force=False):
    """The reports with no summary object anywhere, oldest-first.

    Takes an already-scanned list rather than a directory. The caller picks
    which tree to read -- the S3 mirror normally, the local checkout as a
    fallback -- and scanning there and filtering here keeps that choice in
    one place instead of being re-made, possibly differently, on a second
    scan.

    This is what makes the command input-free, and therefore idempotent and
    self-healing: the event trigger and the scheduled backstop both land
    here and neither can double-summarise, and a summary missed because the
    model was down is picked up by the next trigger rather than being lost.

    `summaries_dirs` takes a single path or an iterable of them; see
    `has_summary`. `--force` keeps the filters but drops the has-a-summary
    test, which is the only way to re-render an existing summary (after a
    prompt change, say). `--day`/`--week` are debugging and backfill filters;
    the scheduled path passes neither. There is deliberately no season
    filter: "every report with no summary" is the whole contract, and
    `priors` already refuses to hand one season's report another's context.
    """
    if isinstance(summaries_dirs, (str, Path)):
        summaries_dirs = (summaries_dirs,)

    if day is not None:
        candidates = [r for r in candidates if r.day == day]
    if week is not None:
        candidates = [r for r in candidates if r.week == week]
    if force:
        return candidates
    return [r for r in candidates if not has_summary(summaries_dirs, r)]


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
