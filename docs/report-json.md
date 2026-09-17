# Report JSON

Every report in `docs/report-weekly-schedule.md` is written twice. The
markdown at `reports/<season>/week-NN/<stem>.md` is unchanged and remains
the artifact a person reads. Beside it, `reports-json/<season>/week-NN/<stem>.json`
carries the same report as structured data: tables as arrays of typed row
objects, prose as strings, section order preserved, and the markdown itself
embedded verbatim.

The JSON exists because markdown is the wrong shape for anything that is not
a person. A consumer cannot sort the practice-report table, chart line
movement across weeks, badge a stale feed, or pull "decisions due" apart
from the tables that support it — all of which are already distinct,
computed objects inside each day module's `build()` a moment before they are
flattened into pipe-table text.

Both artifacts come from one `build()` call on one set of values, so they
cannot disagree about what the report says. The JSON is a strict superset:
whatever the structuring failed to capture is still readable out of
`markdown`, and `markdown_sha256` says whether that embedded copy is the
same bytes as the `.md` in the bucket.

## How to read this

Tagged **Observed** (measured from an on-disk artifact), **Documented**
(asserted by a vendor or a docstring in this repo), or **Inferred** (deduced
from how the code behaves, not published anywhere), per `CLAUDE.md`.

## The envelope

| Key | What it is |
|---|---|
| `schema_version` | `1`. Additive-only from here, following `espn_ff/ai/summarize.py`'s convention |
| `season`, `week` | The render's season and scoring period |
| `day` | The `cli.REPORTS` key — `tuesday-waivers`, not `tuesday` |
| `day_label`, `slug` | The two filename segments; `day_label` is shared by both Tuesday reports |
| `stem` | `YYYY-MM-DD-<day_label>-<slug>` — the join key across all three report prefixes |
| `generated_at` | ISO-8601 ET, stamped when the envelope was written |
| `header` | The dateline: `title`, `covers`, `week`, `week_window`, `rendered_at`, `rendered_display` |
| `sections` | The complete report body, in render order. See below |
| `markdown` | The `.md` file's exact bytes |
| `markdown_sha256` | SHA-256 of `markdown` |
| `related` | `markdown_path` and `summary_path`, as bucket keys |

## Sections

`sections` is the whole body in order, including the freshness block and the
closing blind-spot list — one uniform list rather than some parts hoisted and
some not. `header` sits outside it because the dateline is not a `##`
section.

Every section carries `id`, `heading`, `level` and `kind`, plus an optional
`data` object. Six kinds:

| `kind` | Payload | Used for |
|---|---|---|
| `freshness` | `feeds[]` of `{feed, at, at_display, stale}`, plus `notes` | The per-feed header every report opens with |
| `table` | `columns[]`, `rows[]`, `notes[]` | Any pipe table |
| `prose` | `body[]` of lines, `emphasis` | Paragraphs and bullet lists that are not tabular |
| `list` | `items[]` | Bulleted blocks, notably the closing "What this report cannot see" |
| `insufficient` | `reason` | A section that could not be computed |
| `blocks` | `blocks[]` of further sections | Composition — see below |

`blocks` is the single composite kind and it recurses. It covers two
different markdown shapes: `###`/`####` nesting, where each child carries its
own `heading` and `level` (Monday's Alternatives, the waiver report's Add
candidates, Wednesday's Waiver outcomes, Sunday's undecided slots); and
several blocks under one `##` with no subheadings of their own, where the
children carry `heading: null` (Wednesday's Watchlist prints a table, an
italic note, then a second table).

### Section ids are stable; headings are not

Two reports build their headings at render time — Saturday's
`## Tier changes since <baseline date>` and Thursday's
`## Canonical usage -- week N`. **Route on `id`, display `heading`.** A
consumer matching on heading text breaks on Saturday every day and on
Thursday every week. *(Observed — `espn_ff/report/saturday.py`,
`espn_ff/report/thursday.py`.)*

### `data` carries what the prose states in English

Several numbers exist in the markdown only inside a sentence: Wednesday's
waiver-outcome counts, Tuesday's "points left on the table", Friday's
held-open slots, Sunday's minutes-to-kickoff and its verdict on whether this
morning's odds job actually produced the lines being shown. Those ride in the
section's `data` object so a consumer does not have to parse the sentence.

`data` is not a summary of the rows — it is the facts the rows do not hold.

## Values are raw; placeholders are null

`render.py` has a display vocabulary for "no reading": `render.num` emits the
literal string `"insufficient data"`, `render.table` emits `--`, and
`sleeper_signals.format_trajectory` emits an em dash per missing day. A third
source is upstream rather than cosmetic — `availability.read` writes
`"insufficient data"` into the `tier` column itself when nflverse has no row
for the week *(Observed — `espn_ff/report/availability.py`)*.

**In a JSON table cell all of these are `null`.** A consumer handed
`"insufficient data"` in a tier column either renders the words as a tier or
tries to parse them as one. `espn_ff/report/payload.py:unset` applies this to
every cell and every `data` scalar; `PLACEHOLDERS` is the authoritative set,
and `tests/payload_helpers.py` imports it rather than restating it.

Prose bodies and section notes keep those strings verbatim — there the words
are the report's own phrasing, and stripping them would empty the sentence.

Two other cases follow from the same rule:

- **A bye week's `offense_pct` is `null`, not `0.0`.** Thursday's canonical
  usage table suppresses it, and a consumer plotting the raw column would
  otherwise draw a cliff that never happened.
- **A count that could not be computed is `null`, never `0`.** Sunday's
  `out_starter_count` is null when the overnight tier read did not run,
  because "no starters are out" and "we could not find out" are opposite
  answers. This is `docs/report-weekly-schedule.md`'s binding rule — a stale
  or missing input renders the words, not a number — carried into JSON.

The `insufficient` section kind exists for the same reason at section scope:
"could not compute this, and here is why" is not an empty table.

The one deliberate exception to raw-values-only is the header and freshness
blocks, which carry both the epoch and the ET display string the markdown
showed. `render._fmt_ts`'s formatting is genuinely lossy, and a reader
comparing the two artifacts should not have to reconstruct it.

## Storage

`reports-json/` is gitignored and lives only in S3, unlike the markdown in
`reports/` beside it. `report.yml` writes both from one command, then pushes
them with deliberately opposite verbs:

| Prefix | Verb | `--delete`? | Why |
|---|---|---|---|
| `reports/` | `sync-reports` | yes | Git-tracked; `actions/checkout` restores the complete tree, so the local copy is authoritative |
| `reports-json/` | `sync-reports-json` | **no** | Gitignored; a runner's tree holds only the report this run rendered |

A `--delete` mirror from `reports-json/` would wipe the season's JSON from
the bucket on every single run — the same reasoning
`scripts/s3_sync.sh:cmd_sync_summaries` already documents, and the reason the
JSON is a sibling *directory* rather than a `.json` next to the `.md`: a file
inside `reports/` would be swept up by that workflow's `git add reports/` and
would inherit the `--delete` mirror. `tests/test_workflow_contract.py` pins
both halves.

No IAM or lifecycle change was needed. `infra/s3-policy.json.example` grants
object access on `<BUCKET>/*` with no per-prefix scoping, and no lifecycle
rule covers `reports/` or `summaries/` either.

## Joining a report to its summary

All three prefixes share `<season>/week-NN/<stem>`, so any one of them
addresses the other two with no lookup:

```
reports/2026/week-02/2026-09-16-wednesday-availability-watchlist.md
reports-json/2026/week-02/2026-09-16-wednesday-availability-watchlist.json
summaries/2026/week-02/2026-09-16-wednesday-availability-watchlist.json
```

`related.summary_path` in the envelope names the third of those. **It is
derived, not observed.** The report JSON is written at render time, hours
before `summary.yml` runs and possibly before a summary is ever generated, so
that key says where the envelope will live if and when one is produced — it
is not a claim that the file exists. `related.markdown_path` is the opposite:
the same command wrote that file moments earlier.

Nothing in `espn_ff/ai/` changed, and the summary envelope's
`schema_version` is still 2. Adding a `report_json` key there would mean
asserting the existence of a file the summary run never reads. The summarizer
is also unaffected by construction: `espn_ff/ai/reports.py:scan` globs
`*.md` under `reports/` only.

## Implementation

`espn_ff/report/payload.py` is the shared helper module — the JSON twin of
`render.py`, with the same no-disk-access contract. Each `<day>.py` has a
`payload(...)` beside its `render(...)` taking the **identical** argument
list, and `build()` calls both:

```python
args = (season, week, team_id, ...)
kwargs = {"window": weeks.week_window(season, week), "rendered_at": time.time()}
header, sections = payload(*args, **kwargs)
return payload_lib.RenderedReport(
    render(*args, **kwargs), {"header": header, "sections": sections}
)
```

`rendered_at` is pinned in `build()` rather than left to each emitter's
`time.time()` default. Two independent defaults would stamp the markdown and
the JSON that embeds it with different clock reads on every run.

Two emitters over one set of values can still drift on *which* sections they
emit, which is the one failure mode this design introduces.
`tests/payload_helpers.py:assert_payload_matches_markdown` is the guard: it
walks both and requires the same headings, at the same levels, in the same
order. Every day module's test calls it against at least an empty and a
populated argument set, alongside a JSON-serializability check (a single
leaked `numpy.float64` fails the real write at the last step of a scheduled
run) and the placeholder check above.

## Known gaps

- **No backfill.** The four reports rendered before this existed have no
  JSON, and there is no command to produce one from markdown. Writing a
  markdown parser would yield strictly less structure than a render-time
  payload and would rot against the templates.
- **No `pull-reports-json`.** Nothing in this repo reads the JSON back, so
  there is no read cache and no idempotency check of the kind
  `espn_ff/ai/reports.py:existing_envelope` provides for summaries. A
  re-render of the same day simply overwrites its object.
- **The bucket is private.** This makes the data ingestible, not reachable.
  There is no public read path, no CloudFront distribution and no CORS
  configuration in `infra/`, so a front end still needs a distribution
  decision that nothing here makes.
- **Column `type` is declared, not validated.** Each column names the
  semantic type a consumer should expect, but nothing asserts the rows match
  it — the nearest precedent, `espn_ff/nflverse/datasets.py:assert_schema`,
  checks column presence rather than types.
- **`data` keys are per-section and undocumented individually.** They are
  stable within a schema version but the set is not enumerated here; read the
  day module's `payload()` for the section you care about.
- **Structure is not versioned per section.** `schema_version` covers the
  envelope. A section that gains a column or a `data` key does so silently
  within version 1, which is safe for additive changes and would not be for
  a rename.
