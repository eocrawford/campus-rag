"""Source selection: load config/sources.toml and choose page URLs from sitemaps."""

import re
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any
from xml.etree import ElementTree

from campus_rag.config import find_repo_root
from campus_rag.ingest.models import SourceConfig

if TYPE_CHECKING:
    from campus_rag.ingest.fetch import PoliteFetcher

_SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


class SourceConfigError(Exception):
    """Raised when the source configuration or a root sitemap is unusable."""


def load_source_configs(path: Path | None = None) -> list[SourceConfig]:
    """Load and validate the `[[sources]]` tables. Defaults to <repo root>/config/sources.toml."""
    if path is None:
        root = find_repo_root()
        if root is None:
            raise SourceConfigError("cannot find the repository root (no pyproject.toml)")
        path = root / "config" / "sources.toml"
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except OSError as exc:
        raise SourceConfigError(f"{path}: cannot read file: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise SourceConfigError(f"{path}: invalid TOML: {exc}") from exc

    entries = data.get("sources")
    if not isinstance(entries, list) or not entries:
        raise SourceConfigError(f"{path}: expected at least one [[sources]] table")

    configs: list[SourceConfig] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise SourceConfigError(f"{path}: sources[{index}] must be a table")
        config = _parse_source(path, index, entry)
        if config.name in seen:
            raise SourceConfigError(f"{path}: duplicate source name {config.name!r}")
        seen.add(config.name)
        configs.append(config)
    return configs


def _parse_source(path: Path, index: int, entry: dict[str, Any]) -> SourceConfig:
    where = f"{path}: sources[{index}]"

    def require(field: str) -> Any:
        if field not in entry:
            raise SourceConfigError(f"{where}: missing required field '{field}'")
        return entry[field]

    def string(field: str) -> str:
        value = require(field)
        if not isinstance(value, str) or not value.strip():
            raise SourceConfigError(f"{where}: field '{field}' must be a non-empty string")
        return value

    def string_list(field: str) -> tuple[str, ...]:
        value = require(field)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise SourceConfigError(f"{where}: field '{field}' must be a list of strings")
        return tuple(value)

    def integer(field: str, minimum: int) -> int:
        value = require(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise SourceConfigError(f"{where}: field '{field}' must be an integer >= {minimum}")
        return value

    name = string("name")
    sitemap_url = string("sitemap_url")
    include_prefixes = string_list("include_prefixes")
    exclude_patterns = string_list("exclude_patterns")
    for pattern in exclude_patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise SourceConfigError(
                f"{where}: field 'exclude_patterns' has an invalid regex {pattern!r}: {exc}"
            ) from exc
    min_words = integer("min_words", 0)
    max_pages = integer("max_pages", 1) if "max_pages" in entry else None

    return SourceConfig(
        name=name,
        sitemap_url=sitemap_url,
        include_prefixes=include_prefixes,
        exclude_patterns=exclude_patterns,
        min_words=min_words,
        max_pages=max_pages,
    )


def parse_sitemap(xml_text: str) -> tuple[list[str], list[str]]:
    """Return (child sitemap URLs, page URLs) from a sitemap index or a URL set."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise ValueError(f"malformed sitemap XML: {exc}") from exc

    if root.tag == f"{_SITEMAP_NS}sitemapindex":
        return _locs(root, "sitemap"), []
    if root.tag == f"{_SITEMAP_NS}urlset":
        return [], _locs(root, "url")
    raise ValueError(f"unexpected sitemap root element: {root.tag}")


def _locs(root: ElementTree.Element, child_tag: str) -> list[str]:
    urls: list[str] = []
    for child in root.findall(f"{_SITEMAP_NS}{child_tag}"):
        loc = child.find(f"{_SITEMAP_NS}loc")
        if loc is not None and loc.text and loc.text.strip():
            urls.append(loc.text.strip())
    return urls


def select_urls(config: SourceConfig, fetcher: "PoliteFetcher") -> list[str]:
    """Return the sorted page URLs a source selects, applying include/exclude rules and the cap."""
    root_page = fetcher.fetch(config.sitemap_url)
    if root_page.status not in ("ok", "not_modified") or root_page.html is None:
        raise SourceConfigError(
            f"source {config.name!r}: cannot fetch sitemap {config.sitemap_url}: "
            f"{root_page.status} {root_page.error or ''}".rstrip()
        )

    child_sitemaps, page_urls = _parse(config, config.sitemap_url, root_page.html)
    candidates = list(page_urls)
    for child_url in child_sitemaps:
        child_page = fetcher.fetch(child_url)
        if child_page.status not in ("ok", "not_modified") or child_page.html is None:
            raise SourceConfigError(
                f"source {config.name!r}: cannot fetch child sitemap {child_url}: "
                f"{child_page.status} {child_page.error or ''}".rstrip()
            )
        _, child_pages = _parse(config, child_url, child_page.html)
        candidates.extend(child_pages)

    excludes = [re.compile(pattern) for pattern in config.exclude_patterns]
    selected = sorted(
        {
            url
            for url in candidates
            if url.startswith(config.include_prefixes)
            and not any(pattern.search(url) for pattern in excludes)
        }
    )
    if config.max_pages is not None:
        selected = selected[: config.max_pages]
    return selected


def _parse(config: SourceConfig, url: str, xml_text: str) -> tuple[list[str], list[str]]:
    try:
        return parse_sitemap(xml_text)
    except ValueError as exc:
        raise SourceConfigError(f"source {config.name!r}: {url}: {exc}") from exc
