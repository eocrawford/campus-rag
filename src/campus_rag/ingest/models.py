"""Data types shared by the ingestion stages: select, fetch, extract, chunk, store."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

FetchStatus = Literal["ok", "not_modified", "disallowed", "failed"]


@dataclass(frozen=True)
class SourceConfig:
    """Selection rules for one source, loaded from config/sources.toml."""

    name: str
    sitemap_url: str
    include_prefixes: tuple[str, ...]
    exclude_patterns: tuple[str, ...]  # regular expressions matched against the full URL
    min_words: int
    max_pages: int | None


@dataclass(frozen=True)
class FetchedPage:
    """Result of fetching one URL.

    `html` is set when status is "ok" or "not_modified" (served from the local cache).
    """

    url: str
    status: FetchStatus
    html: str | None
    etag: str | None
    last_modified: str | None
    fetched_at: datetime
    error: str | None = None


@dataclass(frozen=True)
class Section:
    """A run of body text under one heading path, in document order."""

    heading_path: tuple[str, ...]  # (page title, h2, h3, ...)
    text: str  # paragraphs, list items, and table rows separated by blank lines


@dataclass(frozen=True)
class ExtractedDocument:
    url: str
    title: str
    sections: tuple[Section, ...]
    word_count: int


@dataclass(frozen=True)
class Chunk:
    ordinal: int  # 0-based position within the document
    heading_path: str  # heading path joined with " > "
    content: str  # heading path line, blank line, then chunk text
    token_count: int  # cl100k_base tokens in `content`
    content_hash: str  # sha256 hex of `content`
