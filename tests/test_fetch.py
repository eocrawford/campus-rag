import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from campus_rag.ingest.fetch import USER_AGENT, PoliteFetcher

PAGE_URL = "https://example.edu/directory/page/"
ROBOTS_URL = "https://example.edu/robots.txt"
ALLOW_ROBOTS = "User-agent: *\nDisallow: /wp-admin/\n"


class FakeClock:
    """Time advances only through `sleep`, so rate limits are tested without waiting."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Server:
    """Scripted responses per URL; records each request with its clock reading."""

    def __init__(self, clock: FakeClock, routes: dict[str, list]) -> None:
        self.clock = clock
        self.routes = routes
        self.requests: list[tuple[float, httpx.Request]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((self.clock.now, request))
        script = self.routes.get(str(request.url))
        if script is None:
            return httpx.Response(404)
        item = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(item, Exception):
            raise item
        return item

    def urls(self) -> list[str]:
        return [str(request.url) for _, request in self.requests]


def make_fetcher(tmp_path: Path, routes: dict[str, list], **kwargs):
    clock = FakeClock()
    server = Server(clock, routes)
    fetcher = PoliteFetcher(
        tmp_path / "cache",
        transport=httpx.MockTransport(server),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        **kwargs,
    )
    return fetcher, server, clock


def robots_ok() -> list:
    return [httpx.Response(200, text=ALLOW_ROBOTS)]


def page_response(html: str = "<p>hi</p>", **headers: str) -> httpx.Response:
    return httpx.Response(200, text=html, headers=headers)


def test_ok_fetch_writes_cache_and_sends_user_agent(tmp_path):
    routes = {
        ROBOTS_URL: robots_ok(),
        PAGE_URL: [page_response("<main>Hello</main>", ETag='"v1"', **{"Last-Modified": "Mon"})],
    }
    fetcher, server, _ = make_fetcher(tmp_path, routes)
    with fetcher:
        page = fetcher.fetch(PAGE_URL)

    assert page.status == "ok"
    assert page.html == "<main>Hello</main>"
    assert page.etag == '"v1"'
    assert page.last_modified == "Mon"
    assert page.error is None
    assert page.fetched_at.utcoffset() == timedelta(0)
    assert all(request.headers["User-Agent"] == USER_AGENT for _, request in server.requests)

    cache_files = sorted((tmp_path / "cache").iterdir())
    assert [p.suffix for p in cache_files] == [".html", ".json"]
    assert cache_files[0].read_text(encoding="utf-8") == "<main>Hello</main>"
    meta = json.loads(cache_files[1].read_text(encoding="utf-8"))
    assert meta["url"] == PAGE_URL
    assert meta["etag"] == '"v1"'
    assert meta["last_modified"] == "Mon"
    assert meta["status_code"] == 200
    assert meta["fetched_at"].endswith("+00:00")
    assert not list((tmp_path / "cache").glob("*.tmp"))


def test_second_fetch_is_conditional_and_serves_cache_on_304(tmp_path):
    routes = {
        ROBOTS_URL: robots_ok(),
        PAGE_URL: [
            page_response("<main>Cached body</main>", ETag='"v1"', **{"Last-Modified": "Mon"}),
            httpx.Response(304),
        ],
    }
    fetcher, server, _ = make_fetcher(tmp_path, routes)
    with fetcher:
        first = fetcher.fetch(PAGE_URL)
        second = fetcher.fetch(PAGE_URL)

    conditional = server.requests[-1][1]
    assert conditional.headers["If-None-Match"] == '"v1"'
    assert conditional.headers["If-Modified-Since"] == "Mon"
    assert second.status == "not_modified"
    assert second.html == "<main>Cached body</main>"
    assert second.etag == '"v1"'
    assert second.last_modified == "Mon"
    assert second.fetched_at >= first.fetched_at
    meta_path = next((tmp_path / "cache").glob("*.json"))
    assert json.loads(meta_path.read_text(encoding="utf-8"))["fetched_at"] == (
        second.fetched_at.isoformat()
    )


def test_first_fetch_sends_no_conditional_headers(tmp_path):
    fetcher, server, _ = make_fetcher(
        tmp_path, {ROBOTS_URL: robots_ok(), PAGE_URL: [page_response()]}
    )
    with fetcher:
        fetcher.fetch(PAGE_URL)
    headers = server.requests[-1][1].headers
    assert "If-None-Match" not in headers
    assert "If-Modified-Since" not in headers


def test_changed_page_updates_cache(tmp_path):
    routes = {
        ROBOTS_URL: robots_ok(),
        PAGE_URL: [page_response("old", ETag='"v1"'), page_response("new", ETag='"v2"')],
    }
    fetcher, _, _ = make_fetcher(tmp_path, routes)
    with fetcher:
        fetcher.fetch(PAGE_URL)
        second = fetcher.fetch(PAGE_URL)
    assert second.status == "ok"
    assert second.html == "new"
    assert next((tmp_path / "cache").glob("*.html")).read_text(encoding="utf-8") == "new"


def test_304_with_missing_cached_html_refetches_unconditionally(tmp_path):
    routes = {
        ROBOTS_URL: robots_ok(),
        PAGE_URL: [
            page_response("first", ETag='"v1"'),
            httpx.Response(304),
            page_response("second", ETag='"v2"'),
        ],
    }
    fetcher, server, _ = make_fetcher(tmp_path, routes)
    with fetcher:
        fetcher.fetch(PAGE_URL)
        next((tmp_path / "cache").glob("*.html")).unlink()
        page = fetcher.fetch(PAGE_URL)

    assert page.status == "ok"
    assert page.html == "second"
    last = server.requests[-1][1]
    assert "If-None-Match" not in last.headers
    assert server.urls().count(PAGE_URL) == 3


def test_rate_limit_spacing_across_requests_and_hosts(tmp_path):
    other = "https://other.example.org/page"
    routes = {
        ROBOTS_URL: robots_ok(),
        PAGE_URL: [page_response()],
        "https://other.example.org/robots.txt": [httpx.Response(404)],
        other: [page_response()],
    }
    fetcher, server, clock = make_fetcher(tmp_path, routes, min_interval_seconds=1.0)
    with fetcher:
        fetcher.fetch(PAGE_URL)
        fetcher.fetch(other)

    times = [t for t, _ in server.requests]
    assert len(times) == 4
    assert all(b - a >= 1.0 for a, b in zip(times, times[1:], strict=False))
    assert clock.sleeps == [1.0, 1.0, 1.0]


def test_rate_limit_accounts_for_elapsed_time(tmp_path):
    fetcher, server, clock = make_fetcher(
        tmp_path, {ROBOTS_URL: robots_ok(), PAGE_URL: [page_response()]}
    )
    with fetcher:
        fetcher.fetch(PAGE_URL)
        clock.now += 5
        fetcher.fetch(PAGE_URL)  # conditional request after a long pause needs no sleep
    assert clock.sleeps == [1.0]


def test_robots_fetched_once_per_host(tmp_path):
    routes = {
        ROBOTS_URL: robots_ok(),
        PAGE_URL: [page_response()],
        "https://example.edu/other": [page_response()],
    }
    fetcher, server, _ = make_fetcher(tmp_path, routes)
    with fetcher:
        fetcher.fetch(PAGE_URL)
        fetcher.fetch("https://example.edu/other")
    assert server.urls().count(ROBOTS_URL) == 1
    assert fetcher.requests_made == 3


def test_robots_disallow_makes_no_page_request(tmp_path):
    routes = {
        ROBOTS_URL: [httpx.Response(200, text="User-agent: *\nDisallow: /directory/\n")],
        PAGE_URL: [page_response()],
    }
    fetcher, server, _ = make_fetcher(tmp_path, routes)
    with fetcher:
        page = fetcher.fetch(PAGE_URL)
    assert page.status == "disallowed"
    assert page.html is None
    assert page.error == "disallowed by robots.txt"
    assert server.urls() == [ROBOTS_URL]
    assert fetcher.requests_made == 1
    assert not (tmp_path / "cache").exists()


def test_robots_rules_for_our_user_agent(tmp_path):
    robots = "User-agent: campus-rag\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
    fetcher, server, _ = make_fetcher(
        tmp_path, {ROBOTS_URL: [httpx.Response(200, text=robots)], PAGE_URL: [page_response()]}
    )
    with fetcher:
        assert fetcher.fetch(PAGE_URL).status == "disallowed"


@pytest.mark.parametrize("status", [404, 410])
def test_robots_missing_allows_all(tmp_path, status):
    fetcher, _, _ = make_fetcher(
        tmp_path, {ROBOTS_URL: [httpx.Response(status)], PAGE_URL: [page_response()]}
    )
    with fetcher:
        assert fetcher.fetch(PAGE_URL).status == "ok"


@pytest.mark.parametrize("status", [401, 403, 500, 503])
def test_robots_error_status_disallows_host(tmp_path, status):
    fetcher, server, _ = make_fetcher(
        tmp_path, {ROBOTS_URL: [httpx.Response(status)], PAGE_URL: [page_response()]}
    )
    with fetcher:
        page = fetcher.fetch(PAGE_URL)
    assert page.status == "disallowed"
    assert server.urls() == [ROBOTS_URL]
    assert "example.edu" in "".join(fetcher.robots_errors)


def test_robots_network_error_disallows_host_for_the_run(tmp_path):
    routes = {ROBOTS_URL: [httpx.ConnectError("boom")], PAGE_URL: [page_response()]}
    fetcher, server, _ = make_fetcher(tmp_path, routes)
    with fetcher:
        assert fetcher.fetch(PAGE_URL).status == "disallowed"
        assert fetcher.fetch(PAGE_URL).status == "disallowed"
    assert server.urls() == [ROBOTS_URL]


def test_429_with_retry_after_then_success(tmp_path):
    routes = {
        ROBOTS_URL: robots_ok(),
        PAGE_URL: [httpx.Response(429, headers={"Retry-After": "7"}), page_response("done")],
    }
    fetcher, server, clock = make_fetcher(tmp_path, routes)
    with fetcher:
        page = fetcher.fetch(PAGE_URL)
    assert page.status == "ok"
    assert page.html == "done"
    assert 7.0 in clock.sleeps
    page_times = [t for t, r in server.requests if str(r.url) == PAGE_URL]
    assert page_times[1] - page_times[0] >= 7.0
    assert fetcher.requests_made == 3


def test_retry_after_is_capped(tmp_path):
    routes = {
        ROBOTS_URL: robots_ok(),
        PAGE_URL: [httpx.Response(503, headers={"Retry-After": "3600"}), page_response()],
    }
    fetcher, _, clock = make_fetcher(tmp_path, routes)
    with fetcher:
        assert fetcher.fetch(PAGE_URL).status == "ok"
    assert max(clock.sleeps) == 60.0


def test_503_exhausts_retries_with_exponential_backoff(tmp_path):
    routes = {ROBOTS_URL: robots_ok(), PAGE_URL: [httpx.Response(503)]}
    fetcher, server, clock = make_fetcher(tmp_path, routes, max_retries=3)
    with fetcher:
        page = fetcher.fetch(PAGE_URL)
    assert page.status == "failed"
    assert page.html is None
    assert page.error == "HTTP 503 after 4 attempts"
    assert server.urls().count(PAGE_URL) == 4
    assert fetcher.requests_made == 5
    assert clock.sleeps[1:] == [1.0, 2.0, 4.0]  # backoff already exceeds the 1 s interval
    assert not (tmp_path / "cache").exists()


def test_404_fails_without_retry(tmp_path):
    routes = {ROBOTS_URL: robots_ok(), PAGE_URL: [httpx.Response(404)]}
    fetcher, server, _ = make_fetcher(tmp_path, routes)
    with fetcher:
        page = fetcher.fetch(PAGE_URL)
    assert page.status == "failed"
    assert page.error == "HTTP 404"
    assert server.urls().count(PAGE_URL) == 1


def test_timeout_is_retried(tmp_path):
    routes = {
        ROBOTS_URL: robots_ok(),
        PAGE_URL: [httpx.ReadTimeout("slow"), httpx.ConnectError("down"), page_response("ok")],
    }
    fetcher, server, _ = make_fetcher(tmp_path, routes)
    with fetcher:
        page = fetcher.fetch(PAGE_URL)
    assert page.status == "ok"
    assert server.urls().count(PAGE_URL) == 3


def test_persistent_timeout_reports_failure(tmp_path):
    routes = {ROBOTS_URL: robots_ok(), PAGE_URL: [httpx.ReadTimeout("slow")]}
    fetcher, _, _ = make_fetcher(tmp_path, routes, max_retries=1)
    with fetcher:
        page = fetcher.fetch(PAGE_URL)
    assert page.status == "failed"
    assert page.error == "timeout after 2 attempts"


def test_redirects_are_followed(tmp_path):
    target = "https://example.edu/directory/moved/"
    routes = {
        ROBOTS_URL: robots_ok(),
        PAGE_URL: [httpx.Response(301, headers={"Location": target})],
        target: [page_response("moved")],
    }
    fetcher, _, _ = make_fetcher(tmp_path, routes)
    with fetcher:
        page = fetcher.fetch(PAGE_URL)
    assert page.status == "ok"
    assert page.url == PAGE_URL
    assert page.html == "moved"
    assert fetcher.requests_made == 3


def test_redirect_loop_fails(tmp_path):
    routes = {
        ROBOTS_URL: robots_ok(),
        PAGE_URL: [httpx.Response(302, headers={"Location": PAGE_URL})],
    }
    fetcher, _, _ = make_fetcher(tmp_path, routes)
    with fetcher:
        page = fetcher.fetch(PAGE_URL)
    assert page.status == "failed"
    assert "TooManyRedirects" in page.error


def test_requests_made_counts_every_request(tmp_path):
    routes = {
        ROBOTS_URL: robots_ok(),
        PAGE_URL: [httpx.Response(500), page_response("x")],
    }
    fetcher, server, _ = make_fetcher(tmp_path, routes)
    assert fetcher.requests_made == 0
    with fetcher:
        fetcher.fetch(PAGE_URL)
    assert fetcher.requests_made == len(server.requests) == 3
