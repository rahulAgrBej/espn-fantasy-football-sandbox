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

# Roughly 2x the OUTPUT_CONTRACT word cap in prompt.py, in tokens. Generous
# enough that a well-behaved response never trips MAX_TOKENS, tight enough
# that a runaway one stops rather than billing indefinitely.
MAX_OUTPUT_TOKENS = 700

# Deterministic-leaning on purpose: two runs over the same report should not
# disagree about what the week's decision is.
TEMPERATURE = 0.2

USAGE_FIELDS = ("promptTokenCount", "candidatesTokenCount", "totalTokenCount")


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
                 max_output_tokens=MAX_OUTPUT_TOKENS, temperature=TEMPERATURE):
        """One turn: a system instruction plus a single user message.

        Returns {"text": str, "usage": {...}}. Raises GeminiError on a
        non-retryable HTTP status, on a prompt blocked before generation,
        and on any `finishReason` other than STOP -- a MAX_TOKENS or safety
        stop returns a truncated summary that reads as complete, which is
        worse than no summary at all.
        """
        url = f"{self.base}/models/{self.model}:generateContent"
        body = {
            "systemInstruction": {"parts": [{"text": system_instruction}]},
            "contents": [{"role": "user", "parts": [{"text": user_text}]}],
            "generationConfig": {
                "maxOutputTokens": max_output_tokens,
                "temperature": temperature,
            },
        }

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

            return self._parse(response.json())

        raise GeminiError(last_error or "request failed")

    @staticmethod
    def _parse(payload):
        feedback = payload.get("promptFeedback") or {}
        if feedback.get("blockReason"):
            raise GeminiError(f"prompt blocked before generation: {feedback['blockReason']}")

        candidates = payload.get("candidates") or []
        if not candidates:
            raise GeminiError("response carried no candidates")

        candidate = candidates[0]
        finish = candidate.get("finishReason")
        if finish and finish != "STOP":
            raise GeminiError(
                f"generation stopped with finishReason={finish} -- refusing to return a "
                "truncated or filtered summary"
            )

        parts = ((candidate.get("content") or {}).get("parts")) or []
        text = "".join(part.get("text") or "" for part in parts).strip()
        if not text:
            raise GeminiError("response carried no text")

        usage = payload.get("usageMetadata") or {}
        return {"text": text, "usage": {field: usage.get(field, 0) for field in USAGE_FIELDS}}
