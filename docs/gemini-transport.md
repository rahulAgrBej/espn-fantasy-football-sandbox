# Gemini transport and surface

`espn_ff/ai/client.py` talks to Gemini over hand-rolled `requests`, against
the legacy `generateContent` endpoint. That choice is recorded in the
module's own docstring (lines 9–13) and was sound when written. Three of its
premises have since moved: `google-genai` reached GA in May 2025 and is now
the vendor-recommended Python library; Google's Interactions API went GA in
June 2026 and is described as the surface all new capability lands on; and
the AI layer itself grew past "a single POST" — `ai/news.py` now sends
Google Search grounding plus a JSON response schema, and hand-parses the
result. This doc re-evaluates both the transport (how we call the API) and
the surface (which endpoint we call), against evidence gathered from the
current code, the `google-genai` 2.24.0 SDK source, the vendor's own docs,
and a five-call live spike, and either re-affirms the current choice or
names what should change. **No production code changed as part of this
work**, except the one docstring citation this doc's own recommendation
calls for.

## How to read this

Cadence, behavioural and parity claims are tagged the same three ways as
`docs/data-sources.md` and `docs/ai-summaries.md`: **Observed** (measured
from a live call or an on-disk artifact in this repo), **Documented**
(asserted by a vendor page or by SDK source code), **Inferred** (deduced
from how the code behaves, not published anywhere).

## The two axes

Separable, and kept separate throughout — either transport can reach either
surface.

| Axis | Today | Alternative |
|---|---|---|
| **Transport** | `requests` + hand-rolled retry/parse | `google-genai` SDK |
| **Surface** | `v1beta/…:generateContent` (legacy) | Interactions API (current) |

The transport question is a maintenance-cost trade. The surface question is
the one with a shelf-life.

## Decision criteria

**Hard** — parity the current code depends on; a failure here kills that
cell outright:

1. `systemInstruction`/`system_instruction` sent as a genuinely separate
   field, so implicit prompt caching sees it as a stable prefix.
2. Google Search grounding combined with a response schema in one call.
3. The billable search-query list readable back from the response.
4. The Search Suggestions rendered-HTML readable back (Google's terms
   require displaying it wherever grounded results are shown).
5. All five usage fields — prompt/candidates/thinking/cached/total tokens.
6. A refusal (`finishReason` != `STOP` or its analogue) distinguishable
   from a real answer.
7. The API key never reaching a URL, an exception, or a log line.

**Soft** — weighed, not disqualifying: dependency footprint, CI install
time, test-suite rewrite cost, lines of hand-rolled parsing removed,
retry-policy compatibility, error-handling compatibility with the exit-0
contract.

## The parity matrix

All four cells pass every hard criterion. Nothing here is a paper hard-fail
— contrary to this plan's own working assumption going in, laid out below.

| Hard criterion | `requests`+generateContent (current) | SDK+generateContent | `requests`+Interactions | SDK+Interactions |
|---|---|---|---|---|
| System instruction separate | Yes (Observed, pinned by `tests/test_ai_client.py:115`) | Yes (Observed — SDK source `models.py:418-420`, live spike path 2) | Yes (would be hand-built the same way) | Yes (Observed — `CreateModelInteractionParam.system_instruction`, live spike path 5) |
| Grounding + schema combine | Yes (Observed, Preview per `client.py:170-174`) | Yes (Observed — live spike path 4; SDK has zero cross-field validation between `tools` and `response_schema`, silent passthrough) | Yes (Documented — Preview, Gemini 3 series, same as generateContent) | Yes (Observed — live spike path 5; Documented Preview per `structured-output` page) |
| Search queries readable | Yes (`groundingMetadata.webSearchQueries`) | Yes (Observed — `grounding_metadata.web_search_queries`, live spike path 4: 1 query) | Yes (`google_search_call.arguments.queries`, Documented) | Yes (Observed — `steps[].arguments.queries`, live spike path 5: 1 query) |
| Search Suggestions HTML readable | Yes (`searchEntryPoint.renderedContent`) | Yes (Observed — `grounding_metadata.search_entry_point.rendered_content`, live spike path 4) | Yes (`google_search_result.search_suggestions`, Documented) | Yes (Observed — `steps[].result[].search_suggestions`, live spike path 5) |
| All 5 usage fields | Yes (`USAGE_FIELDS`, Observed) | Yes (Observed — live spike paths 2 & 4; note below on a doc inconsistency) | Yes (would be hand-parsed the same way) | Yes (Observed — `usage.total_input/output/thought/cached_tokens`, `total_tokens`, live spike path 5) |
| Refusal distinguishable | Yes (`finishReason != STOP`, Observed) | Yes (`FinishReason` enum, Observed — SDK source `types.py:488`; not live-exercised against an actual refusal in this spike) | Yes (would be hand-parsed the same way) | Partial — interaction-level `status` (Observed: `"completed"` in the spike), not a per-candidate enum with `MAX_TOKENS`/`SAFETY` granularity; **not live-exercised against a refusal**, see Known gaps |
| Key never leaked | Yes (Observed, pinned by `tests/test_ai_client.py:141,151`) | Yes (Observed — header injection at `_api_client.py:846`; no `api_key` interpolation found in exceptions/repr in `_api_client.py`/`errors.py`) | Yes (would be built the same way) | Yes (same client, same header path) |

## Findings, per axis

### Transport: **keep `requests`**

Every hard criterion is already met by the current code, so the SDK's only
argument is the soft column — and every soft item there is a cost, not a
saving, once measured:

- **Dependency footprint.** `google-genai` pulls 20 additional packages
  (`anyio`, `distro`, `google-auth` + `pyasn1`/`pyasn1-modules`/
  `cryptography`/`cffi`/`pycparser`, `httpx` + `httpcore`/`h11`/`certifi`,
  `pydantic` + `pydantic-core`/`annotated-types`/`typing-inspection`,
  `tenacity`, `websockets`, `sniffio`) against a runtime surface that today
  has exactly four (`requests`, `pandas`, `duckdb`, `pyarrow`). **Observed**
  via a baseline vs. with-`google-genai` venv: 14 → 34 installed packages,
  296M → 344M on disk (+48M, +20 packages), measured 2026-09-17.
- **CI install time.** `.github/workflows/summary.yml` and `ff-run` key the
  `uv` cache on `pyproject.toml` with no lockfile, so adding a dependency
  invalidates the cache once. **Observed locally** (not on a GitHub-hosted
  runner, so the absolute numbers don't transfer — the relative delta is
  the meaningful figure): a cold `uv venv && uv pip install -e ".[dev]"`
  went from 3.3s to 5.8s with `google-genai` added, roughly +75% wall time,
  driven by the extra packages to resolve and install.
- **Retry-policy collision.** `tests/test_ai_client.py:246-255` asserts
  `ai/client.py`'s retry constants equal `sleeper/client.py`'s exactly. The
  SDK defaults to **zero retries** (`tenacity.stop_after_attempt(1)`)
  unless a caller passes `HttpRetryOptions` — Documented from SDK source
  (`_api_client.py:547-573`). Configured, its `attempts`/`exp_base`/
  `http_status_codes` can be set to match `MAX_ATTEMPTS=4`/`BACKOFF_BASE=1.5`/
  `RETRY_STATUS` closely, but backoff always runs through
  `tenacity.wait_exponential_jitter`, so jitter is structurally always
  applied — only its magnitude is tunable, down to near-zero. Byte-for-byte
  equality with the hand-rolled sleep loop is not achievable, only
  equivalence; the cross-client invariant test would need to change its
  assertion style to keep passing.
- **The silent-failure trap.** `cli.py:713-716` catches
  `(GeminiError, requests.RequestException)` so `cmd_summarize` always
  exits 0. The SDK is **httpx**-based (`_api_client.py:868`); a non-2xx
  response raises `google.genai.errors.APIError`, but an `httpx.
  TimeoutException` or `httpx.ConnectError` is only retried when
  `retry_options` is configured, and even then is retried, not converted
  — it still propagates as a raw httpx exception on final failure.
  Documented from SDK source. A caller catching only `APIError` (as
  `cli.py` would, by analogy to today's `requests.RequestException`) misses
  it, breaking the "`cmd_summarize` always exits 0" contract pinned by
  `tests/test_cli_exit_codes.py:255-299`. A migration would have to map
  both `APIError` and httpx transport errors onto `GeminiError` at the
  client boundary — a real, non-optional piece of engineering, not a
  detail.
- **Test rewrite cost.** `tests/test_ai_client.py` is 422 lines, all built
  on the repo-wide convention of assigning a hand-rolled `FakeSession`/
  `FakeResponse` onto `client.session` — identical in shape to
  `tests/test_odds_client.py`'s `_FakeSession`. That seam disappears with
  the SDK; a replacement would need an httpx `MockTransport` or stubbing
  `client.models.generate_content` directly. The repo has no
  `responses`/`requests-mock`/`unittest.mock` usage anywhere today, so this
  is a new mocking convention, not a drop-in swap.
- **What the SDK would remove**, for balance: `response.parsed` genuinely
  absorbs the hand-rolled work. **Observed** in the live spike (path 4):
  passing `RESPONSE_SCHEMA` as a raw dict (no Pydantic model) to
  `response_schema` returns `resp.parsed` already shaped as
  `{"players": [...]}"` — no fence-stripping, no `json.loads`. That would
  make `news._load`'s fence-stripping and part of `parse_players`'s
  validation dead code. This is real, and it is the strongest argument for
  the SDK — it just doesn't outweigh the costs above on its own.

The original docstring's core claim — "the REST surface used here is a
single POST" — hasn't moved. What changed is the SDK's maturity, and GA
status alone doesn't offset a 20-package dependency add, a broken
cross-client retry invariant, a required error-mapping layer, and a 422-line
test rewrite, when the current code already clears every hard-parity bar.
**Recommendation: keep `requests`.** Revisit if the hand-rolled JSON
parsing in `news.py` becomes a real maintenance burden, or if a future
model changes the `responseFormat`/mime-type handling in a way that breaks
today's enum workaround and the SDK's `response.parsed` would absorb the
fix for free.

### Surface: **stay on `generateContent`; Interactions is the pre-scoped next migration, not a live one**

The vendor's own docs are in direct, stated tension, both quotes gathered
2026-09-17:

- **Migration guide** (`ai.google.dev/gemini-api/docs/migrate-to-interactions`):
  *"While `generateContent` remains fully supported, we recommend the
  Interactions API for all new development."*
- **Interactions overview** (`ai.google.dev/gemini-api/docs/interactions-overview`):
  *"Going forward, all new models, multimodal capabilities, tools, and
  agentic features will launch on the Interactions API."*

Both Documented, both current, and not reconcilable into one story:
`generateContent` is not being sunset, but it is explicitly where new
capability will **not** land. That is the actual shelf-life risk — not an
imminent deprecation, but a slow narrowing of what a `generateContent`-only
integration can ever pick up.

The plan going into this work assumed Interactions parity for grounding was
"the likeliest hard-fail" for this axis, and that a paper reading would
settle it without spending anything. That assumption was wrong. Endpoint:
**`POST https://generativelanguage.googleapis.com/v1beta/interactions`**
(Documented — `ai.google.dev/api/interactions-api`; v1 stable also exists,
no `v1beta2` found anywhere in the SDK source). Grounding + schema
combination is Documented as Preview-supported on Interactions on the same
terms as `generateContent` — Gemini 3 series only
(`ai.google.dev/gemini-api/docs/structured-output`) — and the live spike
(path 5) confirms it end-to-end: a real `client.interactions.create()` call
with `tools=[{"type": "google_search"}]` and a JSON `response_format`
returned a grounded, schema-shaped answer, with the query and the Search
Suggestions HTML both present.

What genuinely differs is shape, not presence. `generateContent` returns
one flat `groundingMetadata` blob per candidate; Interactions restructures a
grounded turn into discrete `steps` — `google_search_call` (the query),
`google_search_result` (`search_suggestions`, the HTML), `thought`, and
`model_output` (whose citations arrive as inline `url_citation`
annotations with character offsets, not `groundingChunks`' flat list +
separate `groundingSupports` span map). Porting `ai/client.py`'s
`_grounding()` and `ai/news.py`'s `parse_players` to Interactions would mean
walking `steps` instead of reading one dict — a real rewrite, but a
mechanical one, not a capability gap.

**Recommendation: stay on `generateContent` now.** The current code has no
gap the vendor's own roadmap forces closing today, and the SDK path
recommendation above means there is no code sitting on Interactions'
doorstep regardless. Revisit when either trigger fires:

- **A model, tool, or agentic feature we want ships Interactions-only** —
  the overview page's own stated policy, so this is a "when," not an "if."
- **Grounding-with-schema parity breaks on `generateContent`** — e.g. the
  Preview combination lapses without promotion to GA, or a future model
  drops the `responseFormat`/mime-type handling `news.py` currently
  depends on.

## What was measured

### The Phase 2 live spike

Five calls against `gemini-3.8-flash`, run 2026-09-17 from a throwaway
`spike/gemini-transport-eval` branch (deleted, never merged;
`scripts/spike_gemini_transport.py` does not exist on `main`). The same
`system`/`user` prompt text — the literal Python strings built by
`summarize.build_prompt` (for the ungrounded pair) and `news.
system_instruction`/`news.user_message` (for the grounded pair) — was
reused across each pair's two calls by construction, not re-derived, so
transport comparisons are against byte-identical input. The grounded/news
calls used a synthetic one-player roster row (not the full week's roster)
to keep query count and spend bounded and predictable for a paper-trail
spike; the prompt-**assembly** code (`news.system_instruction`,
`news.user_message`, `news.response_format`) is still exercised verbatim.

| # | Path | Prompt tokens | Output tokens (candidates+thinking) | Queries |
|---|---|---|---|---|
| 1 | `requests` + generateContent, ungrounded summary (control) | 34,201 | 1,695 | 0 |
| 2 | SDK + generateContent, ungrounded summary | 34,202 | 1,469 | 0 |
| 3 | `requests` + generateContent, grounded news (control) | 1,251 | 1,014 | 1 |
| 4 | SDK + generateContent, grounded news | 1,320 | 1,122 | 1 |
| 5 | SDK + Interactions, grounded news | 1,137 | 748 | 1 |

Prompt-token counts for the control/SDK ungrounded pair differ by exactly 1
token (34,201 vs. 34,202) on identical input text — Observed, unexplained,
and small enough to read as a tokenizer-boundary artifact rather than a
meaningful divergence.

**Budget: 5 calls (of a ~8-call ceiling), 3 grounding queries (of a
20-query ceiling), ~$0.077 in token spend + ~$0.042 query-equivalent spend
(all 3 queries actually landed inside the free 5,000/month allowance, so
real marginal cost was $0.077) — combined worst case ~$0.12 against the
~$0.15 ceiling.** Every hard criterion above marked "Observed — live spike"
came from this run; the raw JSON for all five paths was reviewed and is
excerpted above rather than kept in the tree.

### A doc inconsistency worth flagging, not trusting

The SDK's `GenerateContentResponseUsageMetadata` docstring (`types.py:
8448-8449`) reads *"This data type is not supported in Gemini API"* — which
reads as a copy/paste artifact from a Vertex-only type doc, since the live
spike populated every field on this exact Gemini Developer API call. Treat
SDK docstrings the same way `docs/ai-summaries.md` already treats the
vendor's own REST examples: a starting point, verified against a live call,
not a source of truth on their own.

## Known gaps

- **A refusal was never spiked.** All five live calls in Phase 2 completed
  with `STOP`/`"completed"`. Whether Interactions' `status` field carries
  enough granularity to replace `client.py`'s `finishReason == MAX_TOKENS`
  branch (which names thinking spend vs. answer size in its error message)
  is Documented from the type shape, not Observed from an actual refusal —
  a real gap if this axis is ever revisited for migration.
- **Interactions' citation shape (`url_citation` annotations with character
  offsets) was seen once, on one short answer.** Whether it maps cleanly
  onto `ai/news.py`'s per-player reconciliation at roster scale, where a
  single response covers 6-9 players, is unverified.
- **CI timing numbers are local, not runner-Observed.** `astral-sh/setup-
  uv`'s cache behavior and GitHub-hosted runner network conditions were not
  reproduced; only the relative delta (~75% more wall time for a cold
  install) should be trusted, not the absolute seconds.
- **The retry-equivalence claim is source-read, not Observed.** Whether
  `HttpRetryOptions(jitter=0.0, ...)` actually produces near-deterministic
  backoff was not exercised live — this doc reports what the SDK source
  and its docstrings say, not a measured retry sequence.
- **`generateContent`'s "remains fully supported" carries no stated
  timeline.** Neither vendor page names a sunset date or a deprecation
  window; the surface-axis recommendation above treats this as an open
  question with named triggers rather than a resolved one, per this doc's
  own sourcing discipline.
- **No cost or grounding-budget ledger exists for this evaluation itself**
  beyond the one-time spike spend recorded above; see
  `docs/ai-summaries.md`'s "Two meters" section for the ongoing production
  budget and `docs/odds-budget.md` for why a per-call ledger exists there
  but not here.
