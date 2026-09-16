# Project guidelines

## Documentation (`docs/*.md`)

This repo's markdown docs (`docs/data-sources.md`, `docs/odds-budget.md`,
`docs/data-collection-weekly-schedule.md`) follow a consistent set of
conventions. Follow these when adding to or editing any doc under `docs/`:

- **Filenames**: kebab-case, a descriptive noun phrase — e.g.
  `data-sources.md`, `odds-budget.md`, not `DataSources.md` or
  `data_sources.md`.
- **Sourcing discipline**: tag any non-obvious cadence, freshness, or
  behavioral claim as one of **Observed** (measured from an on-disk
  artifact), **Documented** (asserted by a vendor or a docstring in this
  repo), or **Inferred** (deduced from how the code behaves, not published
  anywhere). Never state a specific cadence or interval for something that
  hasn't actually been measured or documented — say so explicitly instead
  of guessing (see `docs/data-sources.md`'s "How to read this" section and
  its treatment of ESPN, which publishes no cadence contract at all).
- **Structure**: lead with a short framing paragraph, follow with a table
  for skimmable reference, then prose sections that go deeper on
  individual points. Don't put deep explanation in the table itself —
  keep it scannable and push nuance into the prose that follows.
- **Cross-linking, not duplication**: when two docs cover related ground,
  link to the other by relative path (e.g. `docs/data-sources.md` links to
  `docs/odds-budget.md` for the Odds API credit-ledger invariant instead
  of re-explaining it). Restate only what's needed for the current doc's
  own point.
- **Closing "known gaps" section**: end substantial docs with an explicit
  list of what's deferred, unresolved, or a known limitation, rather than
  staying silent about them. Silent gaps read as completeness; call them
  out instead.

These conventions describe existing practice in this repo's docs, not
aspirational rules — check `docs/data-sources.md` and `docs/odds-budget.md`
for concrete examples before writing a new one.

## Running anything that touches `data/`

**Dispatch, don't render locally.** `gh workflow run <name>.yml` is the
default way to produce any artifact — reports included. The workflows
restore state from S3 and run ESPN pulls with `--refresh`; a local clone
does neither, and its `data/` tree is stale the moment a scheduled run
lands. A local run is the debugging fallback, never the way a report or
export gets made.

**If you must run locally, sync first.** Restore state from S3 before
any `export`, `report`, or `features` run, and never commit an artifact
produced from unsynced local data. See `docs/automation.md`'s
"Dispatching vs. running locally" for the commands.
