"""Generated prose summaries of the weekly reports.

A summary is a **separate artifact with a separate lifecycle**: the report
markdown under `reports/` is never modified, and a summary lands as its own
JSON envelope under the bucket's `summaries/` prefix. That split is the
whole point -- a dead, rate-limited or unconfigured model can never corrupt
or block the report it describes.

See docs/ai-summaries.md.
"""
