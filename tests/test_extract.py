import pytest

from campus_rag.ingest.extract import ExtractionError, extract_document

URL = "https://example.edu/directory/policy-1/"

PAGE = """<!DOCTYPE html>
<html><head><title>Ignored</title><script>var head = 1;</script><style>p {}</style></head>
<body>
<nav><a href="/">Home</a> <a href="/about">About</a></nav>
<main>
  <article>
    <header><h1 class="entry-title">Widget&nbsp;Policy:  Safety&nbsp;Rules</h1></header>
    <div class="entry-content">
      <script>var x = "noise";</script>
      <div class="accordion" id="table-of-contents">
        <div class="screen-reader-text">Table of Contents</div>
        <h3><button type="button">Table of Contents</button></h3>
        <ul id="tocList"><li><a href="#1-purpose">1. Purpose</a></li>
        <li><a href="#2-scope">2. Scope</a></li></ul>
      </div>
      <p>An introduction that comes before   the first
         numbered heading.</p>
      <h2 id="1-purpose">1.&nbsp;&nbsp;Purpose</h2>
      <p>This policy sets rules for widgets.<br>Second line of the same paragraph.</p>
      <h2 id="2-scope">2.&nbsp;&nbsp;Scope</h2>
      <p>It covers:</p>
      <ul>
        <li>Alpha units
          <ul><li>Nested <strong>bold</strong> item</li><li>Another nested item</li></ul>
        </li>
        <li><p>Paragraph in item</p><p>Second paragraph in item</p></li>
      </ul>
      <h3>A.&nbsp;Equipment</h3>
      <p>Equipment must be <em>inspected</em> yearly.</p>
      <table>
        <thead><tr><th>Level</th><th>Limit</th></tr></thead>
        <tbody><tr><td>Low</td><td>10&nbsp;units</td></tr><tr><td>High</td><td></td></tr></tbody>
      </table>
      <h4>(1) Details</h4>
      <blockquote>Quoted text here.</blockquote>
      <h3>B. Records</h3>
      <dl><dt>Term</dt><dd>Definition of the term.</dd></dl>
      <pre>line one
  line two</pre>
      <h2>3. Empty heading</h2>
      <h2>4. Revised</h2>
      <p>Revised in 2020.</p>
      <form><p>Subscribe to updates</p></form>
    </div>
  </article>
</main>
<footer>Footer noise</footer>
</body></html>
"""


def test_title_and_noise_removed():
    doc = extract_document(URL, PAGE)
    assert doc.url == URL
    assert doc.title == "Widget Policy: Safety Rules"
    text = "\n".join(section.text for section in doc.sections)
    for noise in ("Home", "noise", "Footer", "Subscribe", "Ignored"):
        assert noise not in text
    assert "Table of Contents" not in text
    assert all("Table of Contents" not in part for s in doc.sections for part in s.heading_path)


def test_content_before_first_h2_uses_title_path():
    first = extract_document(URL, PAGE).sections[0]
    assert first.heading_path == ("Widget Policy: Safety Rules",)
    assert first.text == "An introduction that comes before the first numbered heading."


def test_heading_paths_and_numbering_spacing():
    paths = [s.heading_path for s in extract_document(URL, PAGE).sections]
    title = "Widget Policy: Safety Rules"
    assert paths == [
        (title,),
        (title, "1. Purpose"),
        (title, "2. Scope"),
        (title, "2. Scope", "A. Equipment"),
        (title, "2. Scope", "A. Equipment", "(1) Details"),
        (title, "2. Scope", "B. Records"),
        (title, "4. Revised"),
    ]


def test_br_keeps_line_breaks_within_a_block():
    section = extract_document(URL, PAGE).sections[1]
    assert section.text == "This policy sets rules for widgets.\nSecond line of the same paragraph."


def test_nested_lists_are_not_duplicated():
    scope = extract_document(URL, PAGE).sections[2]
    assert scope.text.split("\n\n") == [
        "It covers:",
        "Alpha units",
        "Nested bold item",
        "Another nested item",
        "Paragraph in item",
        "Second paragraph in item",
    ]


def test_tables_blockquotes_definitions_and_pre():
    sections = extract_document(URL, PAGE).sections
    equipment = sections[3].text.split("\n\n")
    assert equipment == [
        "Equipment must be inspected yearly.",
        "Level | Limit",
        "Low | 10 units",
        "High",
    ]
    assert sections[4].text == "Quoted text here."
    assert sections[5].text.split("\n\n") == [
        "Term",
        "Definition of the term.",
        "line one\nline two",
    ]


def test_empty_sections_are_dropped():
    paths = [s.heading_path[-1] for s in extract_document(URL, PAGE).sections]
    assert "3. Empty heading" not in paths


def test_word_count_sums_section_words():
    doc = extract_document(URL, PAGE)
    assert doc.word_count == sum(len(section.text.split()) for section in doc.sections)
    assert doc.word_count > 40


def test_toc_without_id_is_removed_by_heading():
    html = """<main><h1>Title</h1>
    <div class="toc-wrapper"><h2>Table of Contents</h2>
    <ul><li><a href="#a">First</a></li><li><a href="#b">Second</a></li></ul></div>
    <h2>First</h2><p>Body text.</p></main>"""
    doc = extract_document(URL, html)
    assert [s.heading_path for s in doc.sections] == [("Title", "First")]
    assert doc.sections[0].text == "Body text."


def test_toc_heading_followed_by_link_list_is_removed():
    html = """<main><h1>Title</h1><h2>Table of Contents</h2>
    <ul><li><a href="#a">First</a></li></ul>
    <p>Intro text.</p><h2>First</h2><p>Body text.</p></main>"""
    doc = extract_document(URL, html)
    assert [s.heading_path for s in doc.sections] == [("Title",), ("Title", "First")]
    assert doc.sections[0].text == "Intro text."


def test_only_first_main_is_used():
    html = "<main><h1>One</h1><p>First.</p></main><main><h1>Two</h1><p>Second.</p></main>"
    doc = extract_document(URL, html)
    assert doc.title == "One"
    assert [s.text for s in doc.sections] == ["First."]


def test_missing_main_raises():
    with pytest.raises(ExtractionError, match="no <main> element"):
        extract_document(URL, "<html><body><h1>Title</h1><p>Text</p></body></html>")


def test_missing_h1_raises():
    with pytest.raises(ExtractionError, match="no <h1> title"):
        extract_document(URL, "<html><body><main><h2>Heading</h2><p>Text</p></main></body></html>")


def test_blank_h1_raises():
    with pytest.raises(ExtractionError, match="no <h1> title"):
        extract_document(URL, "<main><h1>  &nbsp; </h1><p>Text</p></main>")


NAVIGATION_PAGE = """<main><article><h1>Title</h1><div class="entry-content">
<h2>1. Rules</h2>
<p>Follow the rules.</p>
<p>For related information, see:</p>
<ul><li>Order <a href="https://example.edu/other/">No. 36</a>, "Inventions"</li></ul>
<p><a href="https://example.edu/directory/policy-1/#top">Back to Top</a></p>
<p><a href="#top">Jump up</a></p>
<p><a href="#"> </a> <a href="#rules">Rules</a></p>
<p>Jump to <a href="#rules">the rules</a> for details.</p>
<p><a href="https://example.edu/other/#top">Elsewhere</a></p>
<p>RETURN TO TOP</p>
<p>Print this page</p>
<p>Share</p>
<p>Print</p>
<p>Top</p>
<div><span>&uarr; Back to top</span></div>
<p>Print the form and share it with the office.</p>
</div></article></main>"""


def test_navigation_blocks_are_dropped():
    doc = extract_document(URL, NAVIGATION_PAGE)
    assert len(doc.sections) == 1
    assert doc.sections[0].text.split("\n\n") == [
        "Follow the rules.",
        "For related information, see:",
        'Order No. 36, "Inventions"',
        "Jump to the rules for details.",
        "Elsewhere",
        "Print the form and share it with the office.",
    ]
