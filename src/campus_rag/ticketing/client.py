"""Client for the ticketing API: the connector the MCP `draft_ticket` tool files tickets through."""

from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, Self

import httpx

_START_HINT = "Start it with: uv run python -m campus_rag.ticketing"


class TicketingError(Exception):
    """The ticketing API is unreachable or rejected the ticket; the message says what to do."""


@dataclass(frozen=True)
class FiledTicket:
    id: int
    url: str
    status: str
    category: str
    summary: str


class Ticketing(Protocol):
    def create_ticket(
        self,
        *,
        category: str,
        summary: str,
        details: str,
        source_question: str | None = None,
        request_id: int | None = None,
    ) -> FiledTicket: ...


class TicketingClient:
    """`Ticketing` over HTTP for the API at `base_url`. Use as a context manager or `close()`."""

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout_seconds, connect=3.0))

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

    def create_ticket(
        self,
        *,
        category: str,
        summary: str,
        details: str,
        source_question: str | None = None,
        request_id: int | None = None,
    ) -> FiledTicket:
        """File an open ticket. Raises `TicketingError` if the API is unreachable or refuses."""
        payload = {
            "category": category,
            "summary": summary,
            "details": details,
            "source_question": source_question,
            "request_id": request_id,
        }
        url = f"{self._base_url}/tickets"
        try:
            response = self._client.post(url, json=payload)
        except httpx.TimeoutException:
            raise TicketingError(f"the ticketing API at {self._base_url} timed out") from None
        except httpx.HTTPError as exc:
            raise TicketingError(
                f"cannot reach the ticketing API at {self._base_url} "
                f"({type(exc).__name__}). {_START_HINT}"
            ) from None
        if response.status_code != 201:
            raise TicketingError(
                f"the ticketing API at {self._base_url} rejected the ticket "
                f"(HTTP {response.status_code}): {_detail(response)}"
            )
        try:
            body = response.json()
            return FiledTicket(
                id=int(body["id"]),
                url=str(body["url"]),
                status=str(body["status"]),
                category=str(body["category"]),
                summary=str(body["summary"]),
            )
        except (ValueError, KeyError, TypeError):
            raise TicketingError(
                f"the ticketing API at {self._base_url} returned an unreadable ticket"
            ) from None


def _detail(response: httpx.Response) -> str:
    try:
        detail = response.json()["detail"]
    except (ValueError, KeyError, TypeError):
        return response.text[:200] or "no details"
    if isinstance(detail, list):  # FastAPI validation errors
        fields = sorted(
            {".".join(str(part) for part in item.get("loc", ())[1:]) for item in detail}
        )
        return "invalid " + ", ".join(fields)
    return str(detail)[:200]
