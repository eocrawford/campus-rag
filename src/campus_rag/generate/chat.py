"""Call an OpenAI-compatible `/chat/completions` API (Ollama by default).

The client retries transient failures, reports token usage and latency, and never logs or prints
the API key.
"""

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Protocol, Self

import httpx

from campus_rag.config import ChatSettings

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

Message = Mapping[str, str]  # {"role": "system" | "user" | "assistant", "content": "..."}


class ChatError(Exception):
    """The chat server is unreachable, lacks the model, rejected a request, or replied badly."""


@dataclass(frozen=True)
class ChatResult:
    text: str
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    elapsed_ms: float


class ChatClient(Protocol):
    def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        response_format: Mapping[str, Any] | None = None,
    ) -> ChatResult: ...


class OpenAICompatibleChat:
    """`ChatClient` backed by `POST {base_url}/chat/completions`. Use as a context manager."""

    def __init__(
        self,
        settings: ChatSettings,
        *,
        client: httpx.Client | None = None,
        max_attempts: int = 3,
        backoff_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._base_url = settings.base_url.rstrip("/")
        self._url = f"{self._base_url}/chat/completions"
        self._headers = {"Authorization": f"Bearer {settings.api_key}"} if settings.api_key else {}
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep
        self._clock = clock
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(settings.timeout_seconds, connect=5.0)
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        response_format: Mapping[str, Any] | None = None,
    ) -> ChatResult:
        """Return the model's reply to `messages`. Raises `ChatError` on failure."""
        payload: dict[str, Any] = {
            "model": model,
            "messages": [dict(message) for message in messages],
            "temperature": temperature,
            "stream": False,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_format is not None:
            payload["response_format"] = dict(response_format)
        started = self._clock()
        response = self._send("POST", self._url, model, self._max_attempts, json=payload)
        elapsed_ms = (self._clock() - started) * 1000
        return self._parse(response, model, elapsed_ms)

    def check(self, model: str) -> None:
        """Prove the server is reachable and serves `model`, without generating text.

        Lists the server's models (`GET /models`). Servers without that endpoint get a one-token
        completion instead. Raises `ChatError` with a fix for the common failures. Does not retry.
        """
        url = f"{self._base_url}/models"
        response = self._send("GET", url, model, 1, allow_404=True)
        if response.status_code == 404:
            probe = {
                "model": model,
                "messages": [{"role": "user", "content": "ok"}],
                "max_tokens": 1,
            }
            self._send("POST", self._url, model, 1, json=probe)
            return
        try:
            available = {str(item["id"]) for item in response.json()["data"]}
        except (ValueError, KeyError, TypeError) as exc:
            raise ChatError(
                f"model list from {url} is not an OpenAI-style payload "
                f"({type(exc).__name__}: {exc})"
            ) from None
        if not _model_available(model, available):
            raise _model_missing_error(url, model, "not in the server's model list")

    def _send(
        self,
        method: str,
        url: str,
        model: str,
        max_attempts: int,
        *,
        allow_404: bool = False,
        json: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        for attempt in range(1, max_attempts + 1):
            try:
                response = self._client.request(method, url, json=json, headers=self._headers)
            except httpx.TransportError as exc:
                if attempt == max_attempts:
                    reason = f"{type(exc).__name__}: {exc}".rstrip(": ")
                    if isinstance(exc, httpx.TimeoutException) and not isinstance(
                        exc, httpx.ConnectTimeout
                    ):
                        raise ChatError(
                            f"chat request to {url} timed out ({reason}, after {attempt} "
                            "attempt(s)). The model server may be busy with another request; "
                            "retry later or raise CAMPUS_RAG_CHAT_TIMEOUT_SECONDS"
                        ) from exc
                    raise ChatError(
                        f"cannot reach the chat server at {url} ({reason}, after {attempt} "
                        "attempt(s)). Start it with: docker compose up -d --wait ollama"
                    ) from exc
            else:
                if response.status_code == 404 and not allow_404:
                    raise _model_missing_error(url, model, f"HTTP 404: {_error_text(response)}")
                if response.status_code not in _RETRYABLE_STATUS:
                    if response.status_code >= 400 and response.status_code != 404:
                        raise ChatError(
                            f"chat request to {url} failed: "
                            f"HTTP {response.status_code}: {_error_text(response)}"
                        )
                    return response
                if attempt == max_attempts:
                    raise ChatError(
                        f"chat request to {url} failed: HTTP {response.status_code} after "
                        f"{attempt} attempt(s): {_error_text(response)}"
                    )
            self._sleep(self._backoff_seconds * 2 ** (attempt - 1))
        raise AssertionError("unreachable")  # pragma: no cover

    def _parse(self, response: httpx.Response, model: str, elapsed_ms: float) -> ChatResult:
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            usage = body.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ChatError(
                f"chat response from {self._url} is not an OpenAI-style completion "
                f"({type(exc).__name__}: {exc})"
            ) from None
        if not isinstance(content, str):
            raise ChatError(f"chat response from {self._url} has no text content")
        return ChatResult(
            text=content,
            model=str(body.get("model") or model),
            prompt_tokens=_int_or_none(usage.get("prompt_tokens")),
            completion_tokens=_int_or_none(usage.get("completion_tokens")),
            elapsed_ms=elapsed_ms,
        )


def _model_available(model: str, available: set[str]) -> bool:
    if model in available:
        return True
    # Ollama lists untagged models with an explicit ":latest" tag.
    return ":" not in model and f"{model}:latest" in available


def _model_missing_error(url: str, model: str, detail: str) -> ChatError:
    return ChatError(
        f"the chat server at {url} does not have model {model!r} ({detail}). "
        f"Pull it with: docker compose exec ollama ollama pull {model}"
    )


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _error_text(response: httpx.Response) -> str:
    """A short description of an error response; never includes request headers."""
    try:
        body: Any = response.json()
    except ValueError:
        return response.text[:200].strip() or response.reason_phrase
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        error = error.get("message")
    return str(error or body)[:200]
