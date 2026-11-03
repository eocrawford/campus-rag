import json
from collections.abc import Callable

import httpx
import pytest

from campus_rag.config import EmbeddingSettings
from campus_rag.index.embed import EmbeddingError, OpenAICompatibleEmbedder

DIMS = 4


def settings(**overrides) -> EmbeddingSettings:
    values = {
        "base_url": "http://embed.test/v1",
        "model": "test-model",
        "dimensions": DIMS,
        "document_prefix": "D: ",
        "query_prefix": "Q: ",
    }
    return EmbeddingSettings(**{**values, **overrides})


def vector_for(text: str) -> list[float]:
    return [float(len(text)), 0.0, 0.0, 1.0]


class Server:
    """Records requests and answers like an OpenAI-compatible /embeddings endpoint."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict] = []
        self.handler = handler or self.ok

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(json.loads(request.content))
        return self.handler(request)

    def ok(self, request: httpx.Request) -> httpx.Response:
        inputs = json.loads(request.content)["input"]
        data = [{"index": i, "embedding": vector_for(text)} for i, text in enumerate(inputs)]
        # Servers may return items in any order; the client must sort by index.
        return httpx.Response(200, json={"data": list(reversed(data))})


def make(
    server: Server, cfg: EmbeddingSettings | None = None, **kwargs
) -> tuple[OpenAICompatibleEmbedder, list[float]]:
    sleeps: list[float] = []
    client = httpx.Client(transport=httpx.MockTransport(server))
    embedder = OpenAICompatibleEmbedder(
        cfg or settings(), client=client, sleep=sleeps.append, backoff_seconds=0.5, **kwargs
    )
    return embedder, sleeps


def test_documents_get_prefix_and_results_follow_input_order() -> None:
    server = Server()
    embedder, _ = make(server)
    vectors = embedder.embed_documents(["a", "bb", "ccc"])
    assert server.bodies == [{"model": "test-model", "input": ["D: a", "D: bb", "D: ccc"]}]
    assert vectors == [vector_for("D: a"), vector_for("D: bb"), vector_for("D: ccc")]
    assert str(server.requests[0].url) == "http://embed.test/v1/embeddings"


def test_query_gets_query_prefix() -> None:
    server = Server()
    embedder, _ = make(server)
    assert embedder.embed_query("hello") == vector_for("Q: hello")
    assert server.bodies[0]["input"] == ["Q: hello"]


def test_batches_of_32_and_order_is_kept_across_batches() -> None:
    server = Server()
    embedder, _ = make(server)
    texts = [f"t{i}" for i in range(70)]
    vectors = embedder.embed_documents(texts)
    assert [len(body["input"]) for body in server.bodies] == [32, 32, 6]
    assert vectors == [vector_for(f"D: {t}") for t in texts]


def test_empty_input_makes_no_request() -> None:
    server = Server()
    embedder, _ = make(server)
    assert embedder.embed_documents([]) == []
    assert server.requests == []


def test_batch_size_is_capped_at_32() -> None:
    with pytest.raises(ValueError, match="batch_size"):
        OpenAICompatibleEmbedder(settings(), batch_size=33)


def test_authorization_header_only_when_key_is_set() -> None:
    server = Server()
    embedder, _ = make(server)
    embedder.embed_query("x")
    assert "authorization" not in server.requests[0].headers

    embedder, _ = make(server, settings(api_key="s3cret"))
    embedder.embed_query("x")
    assert server.requests[1].headers["authorization"] == "Bearer s3cret"


def test_retries_5xx_with_exponential_backoff_then_succeeds() -> None:
    responses = iter([httpx.Response(503, text="busy"), httpx.Response(500, text="oops")])
    server = Server()

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses, None) or server.ok(request)

    server.handler = handler
    embedder, sleeps = make(server)
    assert embedder.embed_query("x") == vector_for("Q: x")
    assert len(server.requests) == 3
    assert sleeps == [0.5, 1.0]


def test_retries_timeouts_and_connect_errors() -> None:
    failures = iter([httpx.ReadTimeout("slow"), httpx.ConnectError("refused")])
    server = Server()

    def handler(request: httpx.Request) -> httpx.Response:
        failure = next(failures, None)
        if failure is not None:
            raise failure
        return server.ok(request)

    server.handler = handler
    embedder, sleeps = make(server)
    assert embedder.embed_query("x") == vector_for("Q: x")
    assert sleeps == [0.5, 1.0]


def test_gives_up_after_max_attempts() -> None:
    server = Server(lambda request: httpx.Response(502, json={"error": "bad gateway"}))
    embedder, sleeps = make(server, max_attempts=3)
    with pytest.raises(EmbeddingError, match=r"HTTP 502 after 3 attempt\(s\)"):
        embedder.embed_query("x")
    assert len(server.requests) == 3
    assert sleeps == [0.5, 1.0]


def test_does_not_retry_client_errors() -> None:
    server = Server(lambda request: httpx.Response(400, json={"error": {"message": "bad input"}}))
    embedder, sleeps = make(server)
    with pytest.raises(EmbeddingError, match="HTTP 400: bad input"):
        embedder.embed_query("x")
    assert len(server.requests) == 1
    assert sleeps == []


def test_rejects_wrong_vector_count() -> None:
    server = Server(
        lambda r: httpx.Response(200, json={"data": [{"index": 0, "embedding": [0] * 4}]})
    )
    embedder, _ = make(server)
    with pytest.raises(EmbeddingError, match=r"1 vector\(s\) for 3 input\(s\)"):
        embedder.embed_documents(["a", "b", "c"])


def test_rejects_duplicate_indexes() -> None:
    data = [{"index": 0, "embedding": [0] * 4}, {"index": 0, "embedding": [1] * 4}]
    server = Server(lambda r: httpx.Response(200, json={"data": data}))
    embedder, _ = make(server)
    with pytest.raises(EmbeddingError, match=r"2 vector\(s\) for 2 input\(s\)"):
        embedder.embed_documents(["a", "b"])


def test_rejects_wrong_vector_length_naming_both_sizes() -> None:
    server = Server(
        lambda r: httpx.Response(200, json={"data": [{"index": 0, "embedding": [0] * 3}]})
    )
    embedder, _ = make(server)
    with pytest.raises(EmbeddingError, match=r"3 dimensions; expected 4"):
        embedder.embed_query("x")


def test_rejects_malformed_payload() -> None:
    server = Server(lambda r: httpx.Response(200, json={"unexpected": True}))
    embedder, _ = make(server)
    with pytest.raises(EmbeddingError, match="not an OpenAI-style embeddings payload"):
        embedder.embed_query("x")


def test_check_succeeds_with_one_small_request() -> None:
    server = Server()
    embedder, _ = make(server)
    embedder.check()
    assert len(server.requests) == 1
    assert len(server.bodies[0]["input"]) == 1


def test_check_reports_unreachable_server_without_retrying() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    embedder, sleeps = make(Server(refuse))
    with pytest.raises(EmbeddingError) as info:
        embedder.check()
    message = str(info.value)
    assert "cannot reach the embedding server at http://embed.test/v1/embeddings" in message
    assert "docker compose up -d --wait ollama" in message
    assert sleeps == []


def test_check_reports_missing_model_on_404() -> None:
    body = {"error": {"message": 'model "test-model" not found, try pulling it first'}}
    embedder, _ = make(Server(lambda r: httpx.Response(404, json=body)))
    with pytest.raises(EmbeddingError) as info:
        embedder.check()
    message = str(info.value)
    assert "does not have model 'test-model'" in message
    assert "docker compose exec ollama ollama pull test-model" in message


def test_check_reports_dimension_mismatch() -> None:
    server = Server(
        lambda r: httpx.Response(200, json={"data": [{"index": 0, "embedding": [0] * 8}]})
    )
    embedder, _ = make(server)
    with pytest.raises(EmbeddingError, match=r"8 dimensions; expected 4"):
        embedder.check()


def test_error_messages_never_contain_the_api_key() -> None:
    server = Server(lambda r: httpx.Response(401, json={"error": "invalid key"}))
    embedder, _ = make(server, settings(api_key="s3cret"))
    with pytest.raises(EmbeddingError) as info:
        embedder.embed_query("x")
    assert "s3cret" not in str(info.value)


def test_context_manager_closes_only_its_own_client() -> None:
    injected = httpx.Client(transport=httpx.MockTransport(Server()))
    with OpenAICompatibleEmbedder(settings(), client=injected):
        pass
    assert not injected.is_closed

    with OpenAICompatibleEmbedder(settings()) as embedder:
        owned = embedder._client
    assert owned.is_closed
