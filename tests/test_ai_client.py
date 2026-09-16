"""The Gemini client's retry policy, its failure modes, and the one thing
that must never be true of it: the key appearing anywhere but a request
header.

`odds/client.py` needs a whole `_redact` layer because The Odds API forces
its key into the URL. This client keeps the key out of the URL entirely, so
the equivalent guarantee is structural rather than defensive -- and these
tests are what pin it as structural rather than incidental.
"""

import json

import pytest
import requests

from espn_ff.ai.client import GeminiClient, GeminiError

KEY = "test-key-not-a-real-credential"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text if text is not None else json.dumps(self._payload)

    def json(self):
        return self._payload


class FakeSession:
    """Records every POST so a test can assert on what actually went out."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "body": json, "timeout": timeout})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def ok_payload(text="A summary.", finish="STOP"):
    return {
        "candidates": [{"finishReason": finish, "content": {"parts": [{"text": text}]}}],
        "usageMetadata": {
            "promptTokenCount": 31000,
            "candidatesTokenCount": 300,
            "totalTokenCount": 31300,
        },
    }


@pytest.fixture
def client(monkeypatch):
    """A client with no config lookup and no real sleeping between retries."""
    monkeypatch.setattr("espn_ff.ai.client.time.sleep", lambda _: None)
    return GeminiClient(api_key=KEY)


def _with(client, *responses):
    """Swap in a scripted session, carrying over the headers the real one was
    constructed with -- those headers are where the credential lives, so a
    fake that dropped them would hide the very thing under test."""
    session = FakeSession(responses)
    session.headers.update(client.session.headers)
    client.session = session
    return session


# --- the happy path -------------------------------------------------------


def test_a_clean_generation_returns_text_and_usage(client):
    _with(client, FakeResponse(payload=ok_payload("Start Gibbs.")))

    result = client.generate("system", "user")

    assert result["text"] == "Start Gibbs."
    # Every USAGE_FIELD is present, zero-filled when the vendor omits it, so
    # a stored envelope always has the same shape to read.
    assert result["usage"] == {
        "promptTokenCount": 31000,
        "candidatesTokenCount": 300,
        "thoughtsTokenCount": 0,
        "cachedContentTokenCount": 0,
        "totalTokenCount": 31300,
    }


def test_multipart_text_is_joined_not_truncated_to_the_first_part(client):
    payload = {
        "candidates": [
            {"finishReason": "STOP", "content": {"parts": [{"text": "one "}, {"text": "two"}]}}
        ]
    }
    _with(client, FakeResponse(payload=payload))

    assert client.generate("system", "user")["text"] == "one two"


def test_missing_usage_metadata_reports_zeros_rather_than_failing(client):
    """The envelope records usage for cost tracking, not for correctness --
    a response without it is still a perfectly good summary."""
    payload = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "hi"}]}}]}
    _with(client, FakeResponse(payload=payload))

    assert client.generate("system", "user")["usage"]["totalTokenCount"] == 0


def test_the_request_body_carries_the_system_instruction_separately(client):
    """Not prepended to the user turn: the system instruction is ~21k tokens
    of unchanging docs and belongs where implicit prompt caching can see it
    as a stable prefix."""
    session = _with(client, FakeResponse(payload=ok_payload()))

    client.generate("SYSTEM TEXT", "USER TEXT")

    body = session.calls[0]["body"]
    assert body["systemInstruction"]["parts"][0]["text"] == "SYSTEM TEXT"
    assert body["contents"][0]["parts"][0]["text"] == "USER TEXT"


def test_the_timeout_is_generous_enough_for_a_generation(client):
    """30s is right for a CSV fetch and wrong for an LLM call; a timeout
    that fires mid-generation looks exactly like an outage."""
    session = _with(client, FakeResponse(payload=ok_payload()))

    client.generate("system", "user")

    assert session.calls[0]["timeout"] == 120


# --- the credential -------------------------------------------------------


def test_the_key_travels_in_a_header_and_never_in_the_url(client):
    session = _with(client, FakeResponse(payload=ok_payload()))

    client.generate("system", "user")

    assert client.session.headers["x-goog-api-key"] == KEY
    assert KEY not in session.calls[0]["url"]
    assert KEY not in json.dumps(session.calls[0]["body"])


def test_the_key_appears_in_no_exception_string(client):
    """Every error path at once. There is no redaction layer here, so the
    guarantee has to come from the key never reaching a formatted string in
    the first place."""
    failures = [
        [FakeResponse(status_code=403, text="forbidden")],
        [FakeResponse(payload={"promptFeedback": {"blockReason": "SAFETY"}})],
        [FakeResponse(payload=ok_payload(finish="MAX_TOKENS"))],
        [FakeResponse(payload={"candidates": []})],
        [FakeResponse(status_code=503)] * 4,
    ]
    for responses in failures:
        _with(client, *responses)
        with pytest.raises(GeminiError) as caught:
            client.generate("system", "user")
        assert KEY not in str(caught.value)


def test_repr_carries_no_credential(client):
    assert KEY not in repr(client)


# --- retries --------------------------------------------------------------


def test_a_503_is_retried_and_then_succeeds(client):
    session = _with(
        client,
        FakeResponse(status_code=503),
        FakeResponse(payload=ok_payload("second time lucky")),
    )

    assert client.generate("system", "user")["text"] == "second time lucky"
    assert len(session.calls) == 2


def test_retries_give_up_after_max_attempts(client):
    session = _with(client, *[FakeResponse(status_code=429)] * 4)

    with pytest.raises(GeminiError, match="after 4 attempts"):
        client.generate("system", "user")

    assert len(session.calls) == 4


def test_a_non_retryable_status_fails_on_the_first_attempt(client):
    """A 403 is a bad key, not a blip. Retrying it four times just delays
    the same answer."""
    session = _with(client, FakeResponse(status_code=403, text="API key not valid"))

    with pytest.raises(GeminiError, match="403"):
        client.generate("system", "user")

    assert len(session.calls) == 1


def test_a_connection_error_propagates_for_the_cli_to_absorb(client):
    """Deliberately not caught here: `cli.cmd_summarize` catches
    requests.RequestException alongside GeminiError and returns 0, so the
    failure is handled once, at the layer that owns the exit code."""
    _with(client, requests.ConnectionError("name or service not known"))

    with pytest.raises(requests.RequestException):
        client.generate("system", "user")


# --- refusing a bad generation -------------------------------------------


@pytest.mark.parametrize("finish", ["MAX_TOKENS", "SAFETY", "RECITATION"])
def test_a_finish_reason_other_than_stop_raises(client, finish):
    """A truncated or filtered summary reads as complete. That is strictly
    worse than no summary -- the report is still there, and the next
    trigger retries."""
    _with(client, FakeResponse(payload=ok_payload("half a sen", finish=finish)))

    with pytest.raises(GeminiError, match=finish):
        client.generate("system", "user")


def test_a_prompt_blocked_before_generation_raises(client):
    _with(client, FakeResponse(payload={"promptFeedback": {"blockReason": "SAFETY"}}))

    with pytest.raises(GeminiError, match="blocked before generation"):
        client.generate("system", "user")


def test_an_empty_text_response_raises_rather_than_writing_an_empty_summary(client):
    payload = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "  "}]}}]}
    _with(client, FakeResponse(payload=payload))

    with pytest.raises(GeminiError, match="no text"):
        client.generate("system", "user")


def test_the_retry_policy_matches_the_shared_one_not_the_metered_one():
    """A retry here costs a fraction of a cent against no quota, so this
    client takes sleeper/nflverse's cheap 4 / 1.5 rather than
    odds/client.py's deliberately expensive 3 / 2.0."""
    from espn_ff.ai import client as ai_client
    from espn_ff.sleeper import client as sleeper_client

    assert ai_client.MAX_ATTEMPTS == sleeper_client.MAX_ATTEMPTS
    assert ai_client.BACKOFF_BASE == sleeper_client.BACKOFF_BASE
    assert ai_client.RETRY_STATUS == sleeper_client.RETRY_STATUS


# --- the output budget covers thinking ------------------------------------


def test_the_output_budget_leaves_room_for_thinking():
    """This model reasons before it writes, and those tokens are spent
    against maxOutputTokens and billed at the output rate. A budget sized
    for the answer alone trips MAX_TOKENS on every call -- which is exactly
    what the first deployed run did, at 700 against 1,742 thinking tokens."""
    from espn_ff.ai import client as ai_client
    from espn_ff.ai import prompt

    # A generous multiple of the prose cap, because thinking dwarfs it.
    assert ai_client.MAX_OUTPUT_TOKENS > prompt.WORD_CAP * 8


def test_thinking_and_cache_tokens_are_recorded(client):
    """Neither is inferable from the other three counts, and both move the
    cost. thoughtsTokenCount is also the number that diagnoses a MAX_TOKENS
    failure, which is otherwise invisible from the stored envelope."""
    payload = ok_payload()
    payload["usageMetadata"]["thoughtsTokenCount"] = 1742
    payload["usageMetadata"]["cachedContentTokenCount"] = 20450
    _with(client, FakeResponse(payload=payload))

    usage = client.generate("system", "user")["usage"]

    assert usage["thoughtsTokenCount"] == 1742
    assert usage["cachedContentTokenCount"] == 20450


def test_a_max_tokens_failure_names_thinking_and_the_cap(client):
    """The first thing to check is thinking, not the word cap -- a summary
    well inside OUTPUT_CONTRACT's limit still trips this. The message has to
    say so, or the next person turns down the prose cap and nothing changes."""
    payload = ok_payload("half a sen", finish="MAX_TOKENS")
    payload["usageMetadata"]["thoughtsTokenCount"] = 690
    _with(client, FakeResponse(payload=payload))

    with pytest.raises(GeminiError) as caught:
        client.generate("system", "user", max_output_tokens=700)

    message = str(caught.value)
    assert "thinking spent 690 tokens" in message
    assert "maxOutputTokens of 700" in message


# --- grounding: the second caller, with the opposite contract -------------


def grounded_payload(text='{"players": []}', queries=("caleb williams injury",), chunks=2,
                     finish="STOP"):
    payload = ok_payload(text, finish=finish)
    payload["candidates"][0]["groundingMetadata"] = {
        "webSearchQueries": list(queries),
        "groundingChunks": [
            {"web": {"uri": f"https://example.test/{n}", "title": f"Source {n}"}}
            for n in range(chunks)
        ],
        "searchEntryPoint": {"renderedContent": "<div>suggestions</div>"},
    }
    return payload


def test_the_summary_request_carries_no_tools_and_no_response_format(client):
    """The load-bearing negative. `ai/prompt.py`'s HOUSE_RULES #8 forbids
    outside knowledge, and a stray search tool on this request would let the
    model satisfy the prompt while quietly violating it -- with no error, no
    warning, and output that reads exactly the same."""
    session = _with(client, FakeResponse(payload=ok_payload()))

    client.generate("system", "user")

    body = session.calls[0]["body"]
    assert "tools" not in body
    assert "responseFormat" not in body["generationConfig"]
    assert client.generate.__defaults__[2] is None, "tools no longer defaults to off"


def test_a_grounded_request_carries_the_search_tool_and_the_schema(client):
    """Built from the real `news.response_format()` rather than a
    hand-written literal. A fake session accepts any value, so hard-coding
    one here would let this test keep passing while teaching the shape the
    live API rejects with a 400 -- the `mimeType` enum trap."""
    from espn_ff.ai import news

    session = _with(client, FakeResponse(payload=grounded_payload()))

    client.generate("system", "user", tools=[{"google_search": {}}],
                    response_format=news.response_format())

    body = session.calls[0]["body"]
    assert body["tools"] == [{"google_search": {}}]
    assert body["generationConfig"]["responseFormat"]["text"]["mimeType"] == "APPLICATION_JSON"


def test_grounding_metadata_is_extracted_from_the_response(client):
    _with(client, FakeResponse(payload=grounded_payload(queries=("a", "b"), chunks=3)))

    result = client.generate("system", "user", tools=[{"google_search": {}}])

    assert result["grounding"]["search_queries"] == ["a", "b"]
    assert len(result["grounding"]["sources"]) == 3
    assert result["grounding"]["sources"][0]["uri"] == "https://example.test/0"
    assert result["grounding"]["search_entry_point"] == "<div>suggestions</div>"


def test_an_ungrounded_response_reports_grounding_as_none(client):
    """None, not an empty dict. `summarize` treats a missing grounding block
    on a call that asked for tools as "the search never fired", which is a
    warning-worthy event -- an empty dict would make it indistinguishable
    from a search that ran and found nothing."""
    _with(client, FakeResponse(payload=ok_payload()))

    assert client.generate("system", "user")["grounding"] is None


def test_empty_search_queries_are_not_counted(client):
    """The vendor ignores empty queries when billing, so recording them
    would overstate the meter the envelope exists to track."""
    _with(client, FakeResponse(payload=grounded_payload(queries=("real", "", "  also real"))))

    result = client.generate("system", "user", tools=[{"google_search": {}}])

    assert result["grounding"]["search_queries"] == ["real", "  also real"]


def test_a_grounding_chunk_with_no_uri_is_dropped(client):
    payload = grounded_payload(chunks=1)
    payload["candidates"][0]["groundingMetadata"]["groundingChunks"].append({"web": {"title": "x"}})
    _with(client, FakeResponse(payload=payload))

    result = client.generate("system", "user", tools=[{"google_search": {}}])

    assert [s["uri"] for s in result["grounding"]["sources"]] == ["https://example.test/0"]


def test_the_key_stays_out_of_the_url_on_the_grounded_path_too(client):
    """The structural guarantee has to hold for both callers, not just the
    one that existed when it was written."""
    session = _with(client, FakeResponse(payload=grounded_payload()))

    client.generate("system", "user", tools=[{"google_search": {}}])

    assert KEY not in session.calls[0]["url"]
    assert session.headers["x-goog-api-key"] == KEY


def test_a_grounded_generation_still_refuses_a_truncated_answer(client):
    """finishReason != STOP must refuse on both paths. A truncated JSON body
    would fail `parse_players` anyway, but failing here names the real cause
    instead of reporting a formatting problem."""
    _with(client, FakeResponse(payload=grounded_payload(finish="MAX_TOKENS")))

    with pytest.raises(GeminiError, match="MAX_TOKENS"):
        client.generate("system", "user", tools=[{"google_search": {}}])


def test_the_news_budget_is_larger_than_the_summary_budget():
    """A grounded call pulls search results into the prompt and reasons over
    them, so it spends more thinking than a summary does -- and thinking is
    charged against this same ceiling. See docs/ai-summaries.md's MAX_TOKENS
    story for what happens when this is sized for the answer alone."""
    from espn_ff.ai import client as client_module

    assert client_module.NEWS_MAX_OUTPUT_TOKENS > client_module.MAX_OUTPUT_TOKENS
