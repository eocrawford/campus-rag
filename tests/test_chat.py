"""Tests for the OpenAI-compatible chat client, using httpx.MockTransport (no network)."""

import json
from collections.abc import Callable

import httpx
import pytest

from campus_rag.config import ChatSettings
from campus_rag.generate.chat import ChatError, OpenAICompatibleChat

MESSAGES = [{"role": "user", "content": "hello"}]


def _completion(text: str = "Hi [1].", **extra) -> dict:
    body = {
        "model": "qwen2.5:7b-instruct",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3},
    }
    body.update(extra)
    return body


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    api_key: str | None = None,
    sleeps: list[float] | None = None,
    max_attempts: int = 3,
) -> OpenAICompatibleChat:
    settings = ChatSettings(base_url="http://chat.test/v1/", api_key=api_key)
    return OpenAICompatibleChat(
        settings,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_attempts=max_attempts,
        sleep=(sleeps.append if sleeps is not None else lambda _: None),
    )


def test_complete_posts_the_request_and_parses_the_reply() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_completion())

    result = _client(handler).complete(MESSAGES, model="qwen2.5:7b-instruct", max_tokens=50)

    assert result.text == "Hi [1]."
    assert result.model == "qwen2.5:7b-instruct"
    assert (result.prompt_tokens, result.completion_tokens) == (12, 3)
    assert result.elapsed_ms >= 0
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "http://chat.test/v1/chat/completions"
    assert json.loads(request.content) == {
        "model": "qwen2.5:7b-instruct",
        "messages": MESSAGES,
        "temperature": 0.0,
        "stream": False,
        "max_tokens": 50,
    }
    assert "authorization" not in request.headers


def test_response_format_is_passed_through() -> None:
    bodies: list[dict] = []
    schema = {"type": "json_schema", "json_schema": {"name": "x", "schema": {"type": "object"}}}

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_completion("{}"))

    _client(handler).complete(MESSAGES, model="m", temperature=0.3, response_format=schema)
    assert bodies[0]["response_format"] == schema
    assert bodies[0]["temperature"] == 0.3
    assert "max_tokens" not in bodies[0]


def test_bearer_token_is_sent_only_with_an_api_key() -> None:
    headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        headers.append(request.headers.get("authorization"))
        return httpx.Response(200, json=_completion())

    _client(handler, api_key="secret-key").complete(MESSAGES, model="m")
    assert headers == ["Bearer secret-key"]


def test_api_key_is_not_in_the_repr_of_settings() -> None:
    assert "secret-key" not in repr(ChatSettings(api_key="secret-key"))


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retryable_statuses_are_retried_with_backoff(status: int) -> None:
    replies = [
        httpx.Response(status, json={"error": "busy"}),
        httpx.Response(200, json=_completion()),
    ]
    sleeps: list[float] = []
    result = _client(lambda request: replies.pop(0), sleeps=sleeps).complete(MESSAGES, model="m")
    assert result.text == "Hi [1]."
    assert sleeps == [1.0]


def test_timeouts_and_connection_errors_are_retried() -> None:
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        if len(attempts) == 2:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json=_completion())

    sleeps: list[float] = []
    assert _client(handler, sleeps=sleeps).complete(MESSAGES, model="m").text == "Hi [1]."
    assert sleeps == [1.0, 2.0]


def test_unreachable_server_error_names_the_fix() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(
        ChatError, match=r"after 3 attempt\(s\).*docker compose up -d --wait ollama"
    ):
        _client(handler).complete(MESSAGES, model="m")


def test_persistent_read_timeout_suggests_a_longer_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(ChatError, match="timed out.*CAMPUS_RAG_CHAT_TIMEOUT_SECONDS"):
        _client(handler).complete(MESSAGES, model="m")


def test_persistent_server_error_reports_status_after_retries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "overloaded"}})

    with pytest.raises(ChatError, match=r"HTTP 503 after 3 attempt\(s\): overloaded"):
        _client(handler).complete(MESSAGES, model="m")


def test_missing_model_error_names_the_pull_command() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404, json={"error": {"message": 'model "m" not found'}})

    with pytest.raises(ChatError, match="docker compose exec ollama ollama pull qwen2.5:7b"):
        _client(handler).complete(MESSAGES, model="qwen2.5:7b")
    assert len(calls) == 1


def test_client_errors_are_not_retried() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, text="bad request body")

    with pytest.raises(ChatError, match="HTTP 400: bad request body"):
        _client(handler).complete(MESSAGES, model="m")
    assert len(calls) == 1


@pytest.mark.parametrize(
    "body",
    [{"choices": []}, {"choices": [{"message": {"content": None}}]}, {"unexpected": True}],
)
def test_malformed_replies_raise_chat_error(body: dict) -> None:
    with pytest.raises(ChatError, match="chat response"):
        _client(lambda request: httpx.Response(200, json=body)).complete(MESSAGES, model="m")


def test_missing_usage_gives_none_token_counts() -> None:
    body = _completion()
    del body["usage"]
    result = _client(lambda request: httpx.Response(200, json=body)).complete(MESSAGES, model="m")
    assert (result.prompt_tokens, result.completion_tokens) == (None, None)


def _models(*ids: str) -> dict:
    return {"object": "list", "data": [{"id": model, "object": "model"} for model in ids]}


def test_check_lists_models_without_generating() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_models("qwen2.5:7b-instruct", "nomic-embed-text:latest"))

    chat = _client(handler)
    chat.check("qwen2.5:7b-instruct")
    chat.check("nomic-embed-text")  # Ollama adds ":latest" to untagged names
    assert [(r.method, str(r.url)) for r in requests] == [("GET", "http://chat.test/v1/models")] * 2


def test_check_reports_a_missing_model_with_the_pull_command() -> None:
    chat = _client(lambda request: httpx.Response(200, json=_models("llama3.1:8b")))
    with pytest.raises(ChatError) as excinfo:
        chat.check("qwen2.5:7b-instruct")
    assert "does not have model 'qwen2.5:7b-instruct'" in str(excinfo.value)
    assert "docker compose exec ollama ollama pull qwen2.5:7b-instruct" in str(excinfo.value)


def test_check_reports_an_unreachable_server_without_retrying() -> None:
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ChatError, match="docker compose up -d --wait ollama"):
        _client(handler, sleeps=sleeps).check("m")
    assert sleeps == []


def test_check_falls_back_to_a_one_token_completion_without_a_models_endpoint() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(404, text="not found")
        return httpx.Response(200, json=_completion("o"))

    _client(handler).check("m")
    assert [r.method for r in requests] == ["GET", "POST"]
    assert json.loads(requests[1].content)["max_tokens"] == 1


def test_owned_client_closes_with_the_context_manager() -> None:
    with OpenAICompatibleChat(ChatSettings()) as chat:
        inner = chat._client
    assert inner.is_closed


def test_max_attempts_must_be_positive() -> None:
    with pytest.raises(ValueError):
        OpenAICompatibleChat(ChatSettings(), max_attempts=0)
