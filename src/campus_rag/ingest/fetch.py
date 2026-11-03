"""Polite HTTP fetching: robots.txt, rate limiting, retries, and a conditional-request cache."""

import hashlib
import json
import os
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from campus_rag.ingest.models import FetchedPage

USER_AGENT = "campus-rag/0.1 (+https://github.com/eocrawford/campus-rag)"

_MAX_RETRY_AFTER_SECONDS = 60.0
_MAX_REDIRECTS = 5


def _is_retryable_status(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


class PoliteFetcher:
    """Fetches pages one at a time, honoring robots.txt and a minimum request interval.

    `fetch` never raises for HTTP or network errors; it returns a `FetchedPage` whose status
    explains the outcome. Fetched HTML and its validators are cached in `cache_dir` so later
    fetches can use conditional requests.
    """

    def __init__(
        self,
        cache_dir: Path,
        *,
        user_agent: str = USER_AGENT,
        min_interval_seconds: float = 1.0,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.user_agent = user_agent
        self.min_interval_seconds = min_interval_seconds
        self.max_retries = max_retries
        self.requests_made = 0
        self.robots_errors: dict[str, str] = {}
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request_start: float | None = None
        self._robots: dict[str, RobotFileParser] = {}
        self._client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=timeout_seconds,
            follow_redirects=False,
            transport=transport,
        )

    def __enter__(self) -> "PoliteFetcher":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def fetch(self, url: str) -> FetchedPage:
        if not self._robots_for(url).can_fetch(self.user_agent, url):
            return FetchedPage(
                url=url,
                status="disallowed",
                html=None,
                etag=None,
                last_modified=None,
                fetched_at=_now(),
                error="disallowed by robots.txt",
            )

        html_path, meta_path = self._cache_paths(url)
        meta = _read_json(meta_path)
        conditional = _conditional_headers(meta)
        outcome = self._get_with_retries(url, conditional)
        if (
            isinstance(outcome, httpx.Response)
            and outcome.status_code == 304
            and conditional
            and not html_path.is_file()
        ):
            conditional = {}
            outcome = self._get_with_retries(url, conditional)

        if isinstance(outcome, str):
            return self._failed(url, outcome)
        response = outcome
        fetched_at = _now()

        if response.status_code == 304 and conditional and html_path.is_file():
            etag = response.headers.get("ETag") or (meta or {}).get("etag")
            last_modified = response.headers.get("Last-Modified") or (meta or {}).get(
                "last_modified"
            )
            self._write_meta(meta_path, url, etag, last_modified, fetched_at, 200)
            return FetchedPage(
                url=url,
                status="not_modified",
                html=html_path.read_text(encoding="utf-8"),
                etag=etag,
                last_modified=last_modified,
                fetched_at=fetched_at,
            )

        if response.status_code == 200:
            html = response.text
            etag = response.headers.get("ETag")
            last_modified = response.headers.get("Last-Modified")
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write(html_path, html)
            self._write_meta(meta_path, url, etag, last_modified, fetched_at, 200)
            return FetchedPage(
                url=url,
                status="ok",
                html=html,
                etag=etag,
                last_modified=last_modified,
                fetched_at=fetched_at,
            )

        return self._failed(url, f"HTTP {response.status_code}")

    def _failed(self, url: str, error: str) -> FetchedPage:
        return FetchedPage(
            url=url,
            status="failed",
            html=None,
            etag=None,
            last_modified=None,
            fetched_at=_now(),
            error=error,
        )

    def _cache_paths(self, url: str) -> tuple[Path, Path]:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.html", self.cache_dir / f"{digest}.json"

    def _write_meta(
        self,
        path: Path,
        url: str,
        etag: str | None,
        last_modified: str | None,
        fetched_at: datetime,
        status_code: int,
    ) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "url": url,
            "etag": etag,
            "last_modified": last_modified,
            "fetched_at": fetched_at.isoformat(),
            "status_code": status_code,
        }
        _atomic_write(path, json.dumps(meta, indent=2))

    def _wait_for_slot(self) -> None:
        if self._last_request_start is not None:
            remaining = self.min_interval_seconds - (self._monotonic() - self._last_request_start)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_start = self._monotonic()

    def _send(self, url: str, headers: dict[str, str]) -> httpx.Response:
        """GET `url`, following redirects by hand so every hop is counted and rate limited."""
        for _ in range(_MAX_REDIRECTS + 1):
            self._wait_for_slot()
            self.requests_made += 1
            response = self._client.get(url, headers=headers)
            if not response.has_redirect_location:
                return response
            url = str(response.url.join(response.headers["Location"]))
        raise httpx.TooManyRedirects(f"more than {_MAX_REDIRECTS} redirects")

    def _get_with_retries(self, url: str, headers: dict[str, str]) -> httpx.Response | str:
        """Return the final response, or an error message when the request cannot succeed."""
        attempts = self.max_retries + 1
        last_error = ""
        for attempt in range(attempts):
            retry_after: float | None = None
            try:
                response = self._send(url, headers)
            except httpx.TransportError as exc:
                last_error = _describe_transport_error(exc)
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                return f"{type(exc).__name__}: {exc}"
            else:
                if not _is_retryable_status(response.status_code):
                    return response
                last_error = f"HTTP {response.status_code}"
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            if attempt < attempts - 1:
                self._sleep(retry_after if retry_after is not None else float(2**attempt))
        return f"{last_error} after {attempts} attempts"

    def _robots_for(self, url: str) -> RobotFileParser:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        parser = self._robots.get(origin)
        if parser is None:
            parser = self._load_robots(origin)
            self._robots[origin] = parser
        return parser

    def _load_robots(self, origin: str) -> RobotFileParser:
        parser = RobotFileParser()
        robots_url = f"{origin}/robots.txt"
        try:
            response = self._send(robots_url, {})
        except httpx.HTTPError as exc:
            self.robots_errors[origin] = (
                f"robots.txt request failed: {_describe_transport_error(exc)}"
            )
            parser.disallow_all = True
            return parser

        status = response.status_code
        if status == 200:
            parser.parse(response.text.splitlines())
        elif status in (404, 410):
            parser.allow_all = True
        elif status in (401, 403):
            self.robots_errors[origin] = f"robots.txt returned HTTP {status}"
            parser.disallow_all = True
        elif status == 429 or status >= 500:
            self.robots_errors[origin] = f"robots.txt returned HTTP {status}"
            parser.disallow_all = True
        else:
            parser.allow_all = True
        return parser


def _now() -> datetime:
    return datetime.now(UTC)


def _conditional_headers(meta: dict | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if meta:
        if meta.get("etag"):
            headers["If-None-Match"] = meta["etag"]
        if meta.get("last_modified"):
            headers["If-Modified-Since"] = meta["last_modified"]
    return headers


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _atomic_write(path: Path, text: str) -> None:
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(text, encoding="utf-8", newline="")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if not value.isdigit():
        return None
    return min(float(int(value)), _MAX_RETRY_AFTER_SECONDS)


def _describe_transport_error(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
