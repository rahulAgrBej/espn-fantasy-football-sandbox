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
    assert result["usage"] == {
        "promptTokenCount": 31000,
        "candidatesTokenCount": 300,
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
