"""HTML extraction: turn a fetched policy page into headed sections of body text."""

import re
from urllib.parse import urldefrag, urljoin

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString, PreformattedString

from campus_rag.ingest.models import ExtractedDocument, Section

_STRIP_TAGS = ("script", "style", "noscript", "template", "svg", "nav", "form", "button", "iframe")
_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_SECTION_LEVELS = {"h2": 1, "h3": 2, "h4": 3}
_BLOCK_TAGS = frozenset(
    {
        "address", "article", "aside", "blockquote", "caption", "dd", "details", "div", "dl",
        "dt", "fieldset", "figcaption", "figure", "footer", "header", "hr", "li", "main", "ol",
        "p", "pre", "section", "summary", "table", "tbody", "tfoot", "thead", "ul",
        *_HEADING_TAGS,
    }
)  # fmt: skip
_TOC_TITLE = "table of contents"
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"))
_SPACES = re.compile(r"\s+")
_NAVIGATION_LABELS = frozenset(
    {"back to top", "top", "return to top", "print", "print this page", "share"}
)
_EDGE_SYMBOLS = re.compile(r"^[\W_]+|[\W_]+$")


class ExtractionError(Exception):
    """Raised when a page lacks the structure the extractor relies on."""


def extract_document(url: str, html: str) -> ExtractedDocument:
    """Extract the title and heading-scoped body sections from a page's `<main>` element."""
    soup = BeautifulSoup(html, "lxml")
    main = soup.find("main")
    if not isinstance(main, Tag):
        raise ExtractionError("no <main> element")
    h1 = main.find("h1")
    title = _inline_text(h1) if isinstance(h1, Tag) else ""
    if not title:
        raise ExtractionError("no <h1> title")

    _remove_table_of_contents(main)
    for tag in main.find_all(_STRIP_TAGS):
        tag.extract()

    walker = _Walker(url, title, skip=h1)
    walker.walk_children(main)
    sections = walker.finish()
    word_count = sum(len(section.text.split()) for section in sections)
    return ExtractedDocument(url=url, title=title, sections=sections, word_count=word_count)


class _Walker:
    """Walks the tree in document order, collecting blocks under the current heading path."""

    def __init__(self, url: str, title: str, skip: Tag) -> None:
        self._url = url
        self._title = title
        self._skip = skip
        self._headings: list[str] = []
        self._inline: list[str] = []
        self._inline_has_content = False  # False while the buffer holds only same-page links
        self._blocks: list[str] = []
        self._sections: list[Section] = []

    def finish(self) -> tuple[Section, ...]:
        self._flush_inline()
        self._close_section()
        return tuple(self._sections)

    def walk_children(self, node: Tag) -> None:
        for child in node.children:
            if isinstance(child, PreformattedString):
                continue  # comments, CDATA, doctype
            if isinstance(child, NavigableString):
                text = _SPACES.sub(" ", str(child))
                self._inline.append(text)
                self._inline_has_content = self._inline_has_content or bool(text.strip())
            elif isinstance(child, Tag):
                self._walk_tag(child)

    def _walk_tag(self, tag: Tag) -> None:
        name = tag.name
        if tag is self._skip:
            return
        if name == "br":
            self._inline.append("\n")
        elif name in _SECTION_LEVELS:
            self._flush_inline()
            self._start_section(tag, _SECTION_LEVELS[name])
        elif name == "tr":
            self._flush_inline()
            self._add_block(_table_row_text(tag))
        elif name == "pre":
            self._flush_inline()
            self._add_block(_clean(tag.get_text()))
        elif name in _BLOCK_TAGS:
            self._flush_inline()
            self.walk_children(tag)
            self._flush_inline()
        elif tag.find(_BLOCK_TAGS) is not None or tag.find("tr") is not None:
            self.walk_children(tag)
        else:
            text = _inline_text(tag, keep_breaks=True)
            self._inline.append(text)
            if text and not self._is_same_page_link_only(tag):
                self._inline_has_content = True

    def _is_same_page_link_only(self, tag: Tag) -> bool:
        """True when all of the tag's text sits inside links that point at this page."""
        strings = [s for s in tag.find_all(string=True) if s.strip()]
        return bool(strings) and all(self._in_same_page_link(s, tag) for s in strings)

    def _in_same_page_link(self, string: NavigableString, scope: Tag) -> bool:
        for ancestor in (string, *string.parents):
            if isinstance(ancestor, Tag) and ancestor.name == "a" and self._links_to_page(ancestor):
                return True
            if ancestor is scope:
                break
        return False

    def _links_to_page(self, anchor: Tag) -> bool:
        href = anchor.get("href")
        if not isinstance(href, str) or "#" not in href:
            return False
        target, _ = urldefrag(urljoin(self._url, href))
        return target.rstrip("/") == urldefrag(self._url)[0].rstrip("/")

    def _start_section(self, heading: Tag, level: int) -> None:
        text = _inline_text(heading)
        if not text:
            return
        self._close_section()
        self._headings = self._headings[: level - 1]
        self._headings.append(text)

    def _flush_inline(self) -> None:
        if self._inline and self._inline_has_content:
            self._add_block(_clean("".join(self._inline)))
        self._inline = []
        self._inline_has_content = False

    def _add_block(self, text: str) -> None:
        if text and not _is_navigation_label(text):
            self._blocks.append(text)

    def _close_section(self) -> None:
        if self._blocks:
            path = (self._title, *self._headings)
            self._sections.append(Section(heading_path=path, text="\n\n".join(self._blocks)))
        self._blocks = []


def _is_navigation_label(text: str) -> bool:
    return _EDGE_SYMBOLS.sub("", text).lower() in _NAVIGATION_LABELS


def _clean(text: str) -> str:
    """Normalize whitespace per line; "\\n" separates lines kept from <br> or <pre>."""
    lines = (_SPACES.sub(" ", line.translate(_INVISIBLE)).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def _inline_text(tag: Tag, keep_breaks: bool = False) -> str:
    parts: list[str] = []
    for node in tag.descendants:
        if isinstance(node, PreformattedString):
            continue
        if isinstance(node, NavigableString):
            parts.append(_SPACES.sub(" ", str(node)))
        elif node.name == "br":
            parts.append("\n" if keep_breaks else " ")
    text = "".join(parts)
    return _clean(text) if keep_breaks else _SPACES.sub(" ", text.translate(_INVISIBLE)).strip()


def _table_row_text(row: Tag) -> str:
    cells = row.find_all(["th", "td"])
    texts = (_inline_text(cell) for cell in cells if cell.find_parent("tr") is row)
    return " | ".join(text for text in texts if text)


def _remove_table_of_contents(main: Tag) -> None:
    for element in main.select("#table-of-contents, #tocDiv, #tocList"):
        element.extract()
    for heading in main.find_all(_HEADING_TAGS):
        if _inline_text(heading).lower() != _TOC_TITLE:
            continue
        node = heading
        while _parent_holds_only(node.parent, main, node):
            node = node.parent
        following = node.find_next_sibling()
        node.extract()
        if node is heading and following is not None and _is_link_list(following):
            following.extract()


def _parent_holds_only(parent: Tag | None, main: Tag, node: Tag) -> bool:
    """True when climbing from `node` to `parent` adds no other headings or paragraphs."""
    if parent is None or parent is main or parent.name in ("article", "body", "html"):
        return False
    watched = [*_HEADING_TAGS, "p"]
    inside = len(node.find_all(watched)) + (1 if node.name in watched else 0)
    return len(parent.find_all(watched)) == inside


def _is_link_list(tag: Tag) -> bool:
    if tag.name not in ("ul", "ol", "div", "nav"):
        return False
    texts = [s for s in tag.find_all(string=True) if s.strip()]
    return bool(texts) and all(s.find_parent("a") is not None for s in texts)
