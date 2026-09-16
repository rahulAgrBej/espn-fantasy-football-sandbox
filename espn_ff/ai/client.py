"""HTTP client for Google's Gemini `generateContent` endpoint.

The fifth module in this repo that touches the network, and the first that
is not a data feed: `espn_ff/client.py` talks to ESPN,
`espn_ff/sleeper/client.py` to Sleeper, `espn_ff/nflverse/client.py` to
nflverse's GitHub release assets, `espn_ff/odds/client.py` to a metered
vendor, and this one to a model.

Deliberately `requests`-based rather than `google-genai`: the REST surface
used here is a single POST, the four clients above already share one retry
idiom, and a fifth that matches them is more consistent than pulling
`google-auth`/`pydantic`/`websockets` into the dependency tree for one
endpoint.

The retry policy mirrors `sleeper/client.py` exactly -- MAX_ATTEMPTS=4,
BACKOFF_BASE=1.5, the same RETRY_STATUS set -- rather than
`odds/client.py`'s deliberately expensive 3 / 2.0. A retry there is a new
billed request against a fixed 500-credit period; a retry here costs a
fraction of a cent against no quota at all, so the cheap policy is correct.

Three differences from the Sleeper client:

- POST with a JSON body, not a parameterised GET.
- Auth via the **`x-goog-api-key` header**, not the `?key=` query parameter
  the vendor's own quickstart uses. `odds/client.py` carries a `_redact`
  layer solely because The Odds API forces its key into the URL; keeping
  the key out of the URL here means there is no URL, exception, or log line
  it can leak through in the first place, so no redaction layer exists and
  none is needed. Nothing below ever interpolates `response.url`.
- `timeout=120`, not 30 -- a generation is not a CSV fetch.

`generate` serves two callers with opposite contracts, on one code path:

  the summary   (`ai/prompt.py`)  no tools, no schema -- forbidden outside
                knowledge, so grounding would defeat the point.
  the news      (`ai/news.py`)    Google Search grounding plus a response
                schema -- it exists to fetch outside knowledge.

`tools` and `response_format` both default to None and are omitted from the
body entirely when falsy, so the summary request stays byte-identical to
what it was before the news layer existed. `tests/test_ai_client.py` pins
that, because a stray `tools` key on the summary call would silently
invalidate HOUSE_RULES #8.
"""

import time

import requests

from .. import config

BASE = "https://generativelanguage.googleapis.com/v1beta"
MODEL = "gemini-3.8-flash"

RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4
BACKOFF_BASE = 1.5
TIMEOUT = 120

# **This budget covers thinking, not just the answer.** That is the whole
# reason it is not ~2x the OUTPUT_CONTRACT word cap: this model reasons
# before it writes, and those tokens are spent against the same ceiling and
# billed at the same output rate. An Observed run spent 1,742 thinking
# tokens to produce a 253-token, 162-word summary -- so a cap sized for the
# answer alone (700 was the first attempt) trips MAX_TOKENS every single
# time and, correctly, stores nothing.
#
# 4000 is ~2x that observed total: enough that a well-behaved response never
# trips the ceiling, tight enough that a runaway one stops rather than
# billing indefinitely. `usage.thoughtsTokenCount` in every envelope is what
# makes the real figure visible rather than guessed at.
MAX_OUTPUT_TOKENS = 4000

# Grounding with Google Search. Snake_case is the spelling the grounding
# docs use; the structured-output page writes the same field `googleSearch`,
# and proto JSON accepts either.
#
# **This tool is metered, and the meter counts queries, not requests.** The
# vendor bills "each search query that the model decides to execute", so one
# call that searches nine players is nine billable uses. 5,000 free per
# month shared across all Gemini 3.x models, then $14/1,000 (Documented --
# Google). `ai/news.py`'s rule 7 asks the model for restraint; nothing
# enforces it, which is why every envelope records the count it actually
# spent. See docs/ai-summaries.md's "The grounding budget".
GOOGLE_SEARCH = [{"google_search": {}}]

# A grounded call retrieves search results INTO the prompt and reasons over
# them, so it spends far more thinking than a summary does. Inferred, not
# Observed -- the same position MAX_OUTPUT_TOKENS was in before it failed in
# production at 700. The finishReason != STOP refusal below means a bad
# guess costs a news block rather than storing a truncated one.
NEWS_MAX_OUTPUT_TOKENS = 8000

# Deterministic-leaning on purpose: two runs over the same report should not
# disagree about what the week's decision is.
TEMPERATURE = 0.2

# `thoughtsTokenCount` and `cachedContentTokenCount` are recorded because
# neither is inferable from the other three, and both move the cost: thinking
# tokens bill at the output rate, and cached prompt tokens bill below the
# input rate. The first is also the number that diagnoses a MAX_TOKENS
# failure, which is otherwise invisible from the envelope.
USAGE_FIELDS = (
    "promptTokenCount",
    "candidatesTokenCount",
    "thoughtsTokenCount",
    "cachedContentTokenCount",
    "totalTokenCount",
)


class GeminiError(RuntimeError):
    """Any non-retryable failure talking to Gemini.

    Deliberately NOT handled in `cli.main`'s except-chain alongside
    EspnError/NflverseError/OddsError: a summary is additive, so
    `cli.cmd_summarize` catches this itself and still returns 0. See
    docs/automation.md's exit-code table.
    """


class GeminiClient:
    def __init__(self, api_key=None, model=MODEL, base=BASE):
        # `base` is injectable so a test -- and the failure-path check in
        # docs/ai-summaries.md -- can point this at an unreachable host
        # without touching the live endpoint.
        self.api_key = api_key or config.gemini_api_key()
        self.model = model
        self.base = base
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": config.USER_AGENT, "x-goog-api-key": self.api_key}
        )

    def __repr__(self):
        # No api_key here, unlike OddsClient.__repr__, which has to redact
        # one. The key lives only in a session header.
        return f"GeminiClient(model={self.model!r})"

    def generate(self, system_instruction, user_text,
                 max_output_tokens=MAX_OUTPUT_TOKENS, temperature=TEMPERATURE,
                 tools=None, response_format=None):
        """One turn: a system instruction plus a single user message.

        Returns {"text": str, "usage": {...}, "grounding": {...} | None}.
        `grounding` is None unless `tools` requested it. Raises GeminiError
        on a non-retryable HTTP status, on a prompt blocked before
        generation, and on any `finishReason` other than STOP -- a
        MAX_TOKENS or safety stop returns a truncated answer that reads as
        complete, which is worse than no answer at all.

        `tools` and `response_format` are omitted from the body when falsy,
        so the summary call's request is unchanged by their existence.
        `response_format` is the `generationConfig.responseFormat` shape --
        {"text": {"mimeType": ..., "schema": ...}} -- and combining it with
        a built-in tool is a **Preview** feature of the Gemini 3 series
        (Documented -- Google). A model outside that series silently loses
        the schema guarantee, which is why `ai/news.py` validates the
        response rather than trusting it.
        """
        url = f"{self.base}/models/{self.model}:generateContent"
        generation_config = {
            "maxOutputTokens": max_output_tokens,
            "temperature": temperature,
        }
        if response_format:
            generation_config["responseFormat"] = response_format

        body = {
            "systemInstruction": {"parts": [{"text": system_instruction}]},
            "contents": [{"role": "user", "parts": [{"text": user_text}]}],
            "generationConfig": generation_config,
        }
        if tools:
            body["tools"] = tools

        last_error = None
        for attempt in range(MAX_ATTEMPTS):
            response = self.session.post(url, json=body, timeout=TIMEOUT)

            if response.status_code in RETRY_STATUS:
                last_error = f"HTTP {response.status_code} from {self.model}:generateContent"
                if attempt < MAX_ATTEMPTS - 1:
                    time.sleep(BACKOFF_BASE ** attempt)
                    continue
                raise GeminiError(f"{last_error} after {MAX_ATTEMPTS} attempts")

            if response.status_code >= 400:
                raise GeminiError(
                    f"HTTP {response.status_code} from {self.model}:generateContent: "
                    f"{response.text[:200]}"
                )

            return self._parse(response.json(), max_output_tokens)

        raise GeminiError(last_error or "request failed")

    @staticmethod
    def _parse(payload, max_output_tokens=MAX_OUTPUT_TOKENS):
        feedback = payload.get("promptFeedback") or {}
        if feedback.get("blockReason"):
            raise GeminiError(f"prompt blocked before generation: {feedback['blockReason']}")

        candidates = payload.get("candidates") or []
        if not candidates:
            raise GeminiError("response carried no candidates")

        candidate = candidates[0]
        finish = candidate.get("finishReason")
        if finish and finish != "STOP":
            usage = payload.get("usageMetadata") or {}
            detail = ""
            if finish == "MAX_TOKENS":
                # Named explicitly because the first thing to check is
                # thinking, not the word cap -- a summary well inside
                # OUTPUT_CONTRACT's limit still trips this if the budget was
                # sized for the answer alone. See MAX_OUTPUT_TOKENS.
                detail = (
                    f" (thinking spent {usage.get('thoughtsTokenCount', 0)} tokens, answer "
                    f"{usage.get('candidatesTokenCount', 0)}, against a "
                    f"maxOutputTokens of {max_output_tokens})"
                )
            raise GeminiError(
                f"generation stopped with finishReason={finish}{detail} -- refusing to "
                "return a truncated or filtered summary"
            )

        parts = ((candidate.get("content") or {}).get("parts")) or []
        text = "".join(part.get("text") or "" for part in parts).strip()
        if not text:
            raise GeminiError("response carried no text")

        usage = payload.get("usageMetadata") or {}
        return {
            "text": text,
            "usage": {field: usage.get(field, 0) for field in USAGE_FIELDS},
            "grounding": _grounding(candidate),
        }


def _grounding(candidate):
    """The citation trail for a grounded call, or None for an ungrounded one.

    Everything here comes from the API's own `groundingMetadata`, never from
    the model's text. A grounded model cites redirect URIs it cannot
    reliably reproduce inline, so a URL the model typed into its answer is
    not evidence that it read anything -- these fields are.

    `search_queries` is the **billable** figure: the vendor bills per query
    the model chose to execute, not per request. `summarize` stores the
    count in every envelope so the monthly projection in
    docs/ai-summaries.md can stop being Inferred.

    `search_entry_point` is the Search Suggestions HTML, carried because
    Google's terms require displaying it wherever grounded results are
    shown. Nothing renders these envelopes yet; storing it is what keeps
    that obligation available to whoever builds the reader.
    """
    metadata = candidate.get("groundingMetadata")
    if not metadata:
        return None

    sources = []
    for chunk in metadata.get("groundingChunks") or []:
        web = chunk.get("web") or {}
        if web.get("uri"):
            sources.append({"uri": web["uri"], "title": web.get("title") or ""})

    # Empty queries are ignored for billing (Documented -- Google), so they
    # are dropped here too rather than inflating the count we record.
    queries = [q for q in (metadata.get("webSearchQueries") or []) if q]

    return {
        "sources": sources,
        "search_queries": queries,
        "search_entry_point": (metadata.get("searchEntryPoint") or {}).get("renderedContent") or "",
    }
