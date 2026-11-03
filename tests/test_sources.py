from datetime import UTC, datetime
from pathlib import Path

import pytest

from campus_rag.ingest.models import FetchedPage, SourceConfig
from campus_rag.ingest.sources import (
    SourceConfigError,
    load_source_configs,
    parse_sitemap,
    select_urls,
)

NS = "http://www.sitemaps.org/schemas/sitemap/0.9"

VALID_TOML = """
[[sources]]
name = "alpha"
sitemap_url = "https://example.edu/sitemap.xml"
include_prefixes = ["https://example.edu/docs/"]
exclude_patterns = ["/archive/", "\\\\.pdf$"]
min_words = 150

[[sources]]
name = "beta"
sitemap_url = "https://beta.example.edu/sitemap.xml"
include_prefixes = ["https://beta.example.edu/"]
exclude_patterns = []
min_words = 0
max_pages = 5
"""


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "sources.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_valid_configs(tmp_path):
    configs = load_source_configs(write_config(tmp_path, VALID_TOML))
    assert [c.name for c in configs] == ["alpha", "beta"]
    alpha, beta = configs
    assert alpha == SourceConfig(
        name="alpha",
        sitemap_url="https://example.edu/sitemap.xml",
        include_prefixes=("https://example.edu/docs/",),
        exclude_patterns=("/archive/", "\\.pdf$"),
        min_words=150,
        max_pages=None,
    )
    assert beta.max_pages == 5


def test_default_path_loads_repository_config():
    configs = load_source_configs()
    assert configs[0].name == "uw-policy"
    assert configs[0].include_prefixes == ("https://policy.uw.edu/directory/",)


@pytest.mark.parametrize(
    "field",
    ["name", "sitemap_url", "include_prefixes", "exclude_patterns", "min_words"],
)
def test_missing_required_field(tmp_path, field):
    lines = [line for line in VALID_TOML.split("[[sources]]")[1].strip().splitlines()]
    kept = "\n".join(line for line in lines if not line.startswith(f"{field} "))
    path = write_config(tmp_path, f"[[sources]]\n{kept}\n")
    with pytest.raises(SourceConfigError, match=f"sources.toml.*missing required field '{field}'"):
        load_source_configs(path)


@pytest.mark.parametrize(
    ("replacement", "field"),
    [
        ("name = 3", "name"),
        ('include_prefixes = "https://example.edu/"', "include_prefixes"),
        ("exclude_patterns = [1]", "exclude_patterns"),
        ("min_words = -1", "min_words"),
        ('min_words = "many"', "min_words"),
        ("min_words = true", "min_words"),
        ("max_pages = 0", "max_pages"),
    ],
)
def test_wrong_field_type(tmp_path, replacement, field):
    key = replacement.split(" = ")[0]
    base = "\n".join(
        line
        for line in VALID_TOML.split("[[sources]]")[1].strip().splitlines()
        if not line.startswith(f"{key} ")
    )
    path = write_config(tmp_path, f"[[sources]]\n{base}\n{replacement}\n")
    with pytest.raises(SourceConfigError, match=f"sources.toml.*'{field}'"):
        load_source_configs(path)


def test_bad_regex_rejected(tmp_path):
    text = VALID_TOML.replace('"/archive/"', '"(unclosed"')
    with pytest.raises(SourceConfigError, match="invalid regex.*unclosed"):
        load_source_configs(write_config(tmp_path, text))


def test_duplicate_name_rejected(tmp_path):
    text = VALID_TOML.replace('name = "beta"', 'name = "alpha"')
    with pytest.raises(SourceConfigError, match="duplicate source name 'alpha'"):
        load_source_configs(write_config(tmp_path, text))


def test_invalid_toml_and_missing_file(tmp_path):
    with pytest.raises(SourceConfigError, match="invalid TOML"):
        load_source_configs(write_config(tmp_path, "[[sources"))
    with pytest.raises(SourceConfigError, match="cannot read"):
        load_source_configs(tmp_path / "missing.toml")


def test_no_sources_rejected(tmp_path):
    with pytest.raises(SourceConfigError, match="at least one"):
        load_source_configs(write_config(tmp_path, "title = 'x'\n"))


def urlset(*urls: str) -> str:
    entries = "".join(f"<url><loc>{u}</loc><lastmod>2026-01-01</lastmod></url>" for u in urls)
    return f'<?xml version="1.0"?><urlset xmlns="{NS}">{entries}</urlset>'


def sitemap_index(*urls: str) -> str:
    entries = "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in urls)
    return f'<?xml version="1.0"?><sitemapindex xmlns="{NS}">{entries}</sitemapindex>'


def test_parse_sitemap_index():
    xml = sitemap_index("https://example.edu/a.xml", " https://example.edu/b.xml ")
    assert parse_sitemap(xml) == (["https://example.edu/a.xml", "https://example.edu/b.xml"], [])


def test_parse_sitemap_urlset():
    xml = urlset("https://example.edu/one/", "https://example.edu/two/")
    assert parse_sitemap(xml) == ([], ["https://example.edu/one/", "https://example.edu/two/"])


def test_parse_sitemap_malformed():
    with pytest.raises(ValueError, match="malformed"):
        parse_sitemap("<urlset><url>")
    with pytest.raises(ValueError, match="unexpected"):
        parse_sitemap("<html></html>")


class FakeFetcher:
    def __init__(self, pages: dict[str, FetchedPage | str]) -> None:
        self.pages = pages
        self.fetched: list[str] = []

    def fetch(self, url: str) -> FetchedPage:
        self.fetched.append(url)
        page = self.pages.get(url)
        if isinstance(page, FetchedPage):
            return page
        if page is None:
            return failed(url, "HTTP 404")
        return ok(url, page)


def ok(url: str, html: str) -> FetchedPage:
    return FetchedPage(url, "ok", html, None, None, datetime.now(UTC))


def failed(url: str, error: str) -> FetchedPage:
    return FetchedPage(url, "failed", None, None, None, datetime.now(UTC), error)


def make_config(**overrides) -> SourceConfig:
    values = {
        "name": "alpha",
        "sitemap_url": "https://example.edu/sitemap.xml",
        "include_prefixes": ("https://example.edu/docs/",),
        "exclude_patterns": (),
        "min_words": 0,
        "max_pages": None,
    }
    return SourceConfig(**(values | overrides))


def test_select_urls_from_index_filters_dedupes_and_sorts():
    fetcher = FakeFetcher(
        {
            "https://example.edu/sitemap.xml": sitemap_index(
                "https://example.edu/s1.xml", "https://example.edu/s2.xml"
            ),
            "https://example.edu/s1.xml": urlset(
                "https://example.edu/docs/b/",
                "https://example.edu/docs/a/",
                "https://example.edu/news/x/",
            ),
            "https://example.edu/s2.xml": urlset(
                "https://example.edu/docs/a/",
                "https://example.edu/docs/archive/old/",
                "https://example.edu/docs/c/",
            ),
        }
    )
    config = make_config(exclude_patterns=("/archive/",))
    assert select_urls(config, fetcher) == [
        "https://example.edu/docs/a/",
        "https://example.edu/docs/b/",
        "https://example.edu/docs/c/",
    ]
    assert fetcher.fetched == [
        "https://example.edu/sitemap.xml",
        "https://example.edu/s1.xml",
        "https://example.edu/s2.xml",
    ]


def test_select_urls_from_plain_urlset():
    fetcher = FakeFetcher(
        {
            "https://example.edu/sitemap.xml": urlset(
                "https://example.edu/docs/z.pdf", "https://example.edu/docs/y/"
            )
        }
    )
    config = make_config(exclude_patterns=(r"\.pdf$",))
    assert select_urls(config, fetcher) == ["https://example.edu/docs/y/"]


def test_select_urls_applies_max_pages_after_filtering():
    fetcher = FakeFetcher(
        {
            "https://example.edu/sitemap.xml": urlset(
                "https://example.edu/aaa/",
                "https://example.edu/docs/c/",
                "https://example.edu/docs/a/",
                "https://example.edu/docs/b/",
            )
        }
    )
    config = make_config(max_pages=2)
    assert select_urls(config, fetcher) == [
        "https://example.edu/docs/a/",
        "https://example.edu/docs/b/",
    ]


def test_select_urls_root_fetch_failure():
    fetcher = FakeFetcher(
        {"https://example.edu/sitemap.xml": failed("https://example.edu/sitemap.xml", "HTTP 503")}
    )
    with pytest.raises(SourceConfigError, match="failed.*HTTP 503"):
        select_urls(make_config(), fetcher)


def test_select_urls_disallowed_root():
    page = FetchedPage(
        "https://example.edu/sitemap.xml",
        "disallowed",
        None,
        None,
        None,
        datetime.now(UTC),
        "disallowed by robots.txt",
    )
    with pytest.raises(SourceConfigError, match="disallowed"):
        select_urls(make_config(), FakeFetcher({"https://example.edu/sitemap.xml": page}))


def test_select_urls_child_failure_is_an_error():
    fetcher = FakeFetcher(
        {"https://example.edu/sitemap.xml": sitemap_index("https://example.edu/missing.xml")}
    )
    with pytest.raises(SourceConfigError, match="child sitemap.*missing.xml"):
        select_urls(make_config(), fetcher)


def test_select_urls_malformed_xml_is_an_error():
    fetcher = FakeFetcher({"https://example.edu/sitemap.xml": "<urlset"})
    with pytest.raises(SourceConfigError, match="malformed"):
        select_urls(make_config(), fetcher)
