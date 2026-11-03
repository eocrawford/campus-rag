import hashlib
import random

import pytest

from campus_rag.ingest.chunk import ELLIPSIS, chunk_document, count_tokens
from campus_rag.ingest.models import Chunk, ExtractedDocument, Section

TITLE = "Example Policy 1.1"
VOCABULARY = (
    "campus office staff member review approval budget committee faculty record request "
    "department schedule notice training equipment facility guideline exception"
).split()


class TextMaker:
    """Deterministic synthetic prose; every sentence carries a unique marker such as S12."""

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.sentences: list[str] = []

    def sentence(self, words: tuple[int, int] = (6, 24)) -> str:
        body = " ".join(self.rng.choice(VOCABULARY) for _ in range(self.rng.randint(*words)))
        text = f"S{len(self.sentences)} {body}."
        self.sentences.append(text)
        return text

    def paragraph(self, sentences: int) -> str:
        return " ".join(self.sentence() for _ in range(sentences))

    def section_text(self, paragraphs: int, sentences: int) -> str:
        return "\n\n".join(self.paragraph(sentences) for _ in range(paragraphs))


def make_doc(*sections: Section, title: str = TITLE) -> ExtractedDocument:
    words = sum(len(section.text.split()) for section in sections)
    return ExtractedDocument(
        url="https://example.test/p", title=title, sections=sections, word_count=words
    )


def body(chunk: Chunk) -> str:
    return chunk.content[len(chunk.heading_path) + 2 :]


def random_doc(seed: int) -> tuple[ExtractedDocument, TextMaker]:
    maker = TextMaker(seed)
    rng = random.Random(seed + 1000)
    sections = []
    for index in range(rng.randint(1, 12)):
        path = (TITLE, f"{index}. Part {index}")
        if rng.random() < 0.5:
            path += (f"{chr(65 + index)}. Detail",)
        shape = rng.choice(["tiny", "small", "medium", "long", "giant-paragraph"])
        if shape == "tiny":
            text = maker.paragraph(1)
        elif shape == "small":
            text = maker.section_text(rng.randint(1, 3), rng.randint(1, 3))
        elif shape == "medium":
            text = maker.section_text(rng.randint(2, 6), rng.randint(2, 6))
        elif shape == "long":
            text = maker.section_text(rng.randint(8, 30), rng.randint(2, 8))
        else:
            text = maker.paragraph(rng.randint(40, 150))
        sections.append(Section(path, text))
    return make_doc(*sections), maker


PARAMETER_SETS = [
    {},
    {"target_tokens": 120, "max_tokens": 200, "overlap_tokens": 30, "min_tokens": 40},
    {"target_tokens": 60, "max_tokens": 64, "overlap_tokens": 20, "min_tokens": 10},
]


@pytest.mark.parametrize("params", PARAMETER_SETS)
@pytest.mark.parametrize("seed", range(12))
def test_invariants_on_generated_documents(seed: int, params: dict) -> None:
    doc, maker = random_doc(seed)
    chunks = chunk_document(doc, **params)
    max_tokens = params.get("max_tokens", 800)

    assert chunks
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        assert chunk.token_count <= max_tokens
        assert chunk.token_count == count_tokens(chunk.content)
        assert chunk.content_hash == hashlib.sha256(chunk.content.encode()).hexdigest()
        assert chunk.content.startswith(chunk.heading_path + "\n\n")
        assert chunk.heading_path.startswith(TITLE)
        assert body(chunk).strip()
    # No text is lost: every sentence survives whole, except sentences that are themselves
    # longer than the budget, whose words all survive.
    everything = "\n".join(c.content for c in chunks)
    for sentence in maker.sentences:
        if sentence not in everything:
            assert count_tokens(sentence) > params.get("target_tokens", 400) // 2
            for word in sentence.split():
                assert word in everything


def test_deterministic() -> None:
    doc, _ = random_doc(3)
    assert chunk_document(doc) == chunk_document(doc)


def test_count_tokens() -> None:
    assert count_tokens("") == 0
    assert count_tokens("hello world") == 2
    assert count_tokens("<|endoftext|>") > 1  # special-token markers count as plain text


def test_section_within_max_is_one_chunk_with_heading_prefix() -> None:
    maker = TextMaker(1)
    text = maker.section_text(paragraphs=6, sentences=5)
    doc = make_doc(Section((TITLE, "2. Scope", "B. Units"), text))
    (chunk,) = chunk_document(doc)
    assert 100 < chunk.token_count <= 800
    assert chunk.heading_path == f"{TITLE} > 2. Scope > B. Units"
    assert chunk.content == f"{TITLE} > 2. Scope > B. Units\n\n{text}"


def test_short_sections_merge_under_common_prefix_and_keep_subheadings() -> None:
    maker = TextMaker(2)
    a, b, c = (maker.paragraph(1) for _ in range(3))
    doc = make_doc(
        Section((TITLE, "3. Duties", "A. Deans"), a),
        Section((TITLE, "3. Duties", "B. Chairs"), b),
        Section((TITLE, "3. Duties", "C. Staff"), c),
    )
    (chunk,) = chunk_document(doc)
    assert chunk.heading_path == f"{TITLE} > 3. Duties"
    assert body(chunk) == f"A. Deans\n\n{a}\n\nB. Chairs\n\n{b}\n\nC. Staff\n\n{c}"


def test_merge_across_top_level_sections_falls_back_to_title() -> None:
    maker = TextMaker(3)
    a, b = maker.paragraph(1), maker.paragraph(1)
    doc = make_doc(Section((TITLE, "1. Purpose"), a), Section((TITLE, "2. Scope", "A. Who"), b))
    (chunk,) = chunk_document(doc)
    assert chunk.heading_path == TITLE
    assert body(chunk) == f"1. Purpose\n\n{a}\n\n2. Scope > A. Who\n\n{b}"


def test_section_equal_to_merge_prefix_adds_no_heading_line() -> None:
    maker = TextMaker(4)
    a, b = maker.paragraph(1), maker.paragraph(1)
    doc = make_doc(Section((TITLE,), a), Section((TITLE, "1. Purpose"), b))
    (chunk,) = chunk_document(doc)
    assert chunk.heading_path == TITLE
    assert body(chunk) == f"{a}\n\n1. Purpose\n\n{b}"


def section_of_size(maker: TextMaker, path: tuple[str, ...], low: int, high: int) -> Section:
    """A one-paragraph section whose content (heading line + body) has low..high tokens."""
    heading = " > ".join(path)
    sentences: list[str] = []
    while count_tokens(f"{heading}\n\n{' '.join(sentences)}") < low:
        sentences.append(maker.sentence((3, 6)))
    section = Section(path, " ".join(sentences))
    assert count_tokens(f"{heading}\n\n{section.text}") <= high
    return section


def test_preamble_merges_into_following_long_section() -> None:
    maker = TextMaker(5)
    preamble = "(Issued under the authority of the department head.)"
    purpose = section_of_size(maker, (TITLE, "1. Purpose"), 300, 360)
    (chunk,) = chunk_document(make_doc(Section((TITLE,), preamble), purpose))
    assert chunk.heading_path == TITLE
    assert body(chunk) == f"{preamble}\n\n1. Purpose\n\n{purpose.text}"


def test_short_section_merges_into_following_long_section() -> None:
    maker = TextMaker(6)
    short = Section((TITLE, "1. Scope", "A. Note"), "See section 4 for details.")
    long = section_of_size(maker, (TITLE, "1. Scope", "B. Units"), 300, 360)
    tail = section_of_size(maker, (TITLE, "2. Roles"), 300, 360)
    chunks = chunk_document(make_doc(short, long, tail))
    assert [c.heading_path for c in chunks] == [f"{TITLE} > 1. Scope", f"{TITLE} > 2. Roles"]
    assert body(chunks[0]) == f"A. Note\n\n{short.text}\n\nB. Units\n\n{long.text}"
    assert body(chunks[1]) == tail.text


def test_last_short_section_merges_into_previous() -> None:
    maker = TextMaker(7)
    first = section_of_size(maker, (TITLE, "1. Policy"), 300, 360)
    history = Section((TITLE, "History"), "Revised July 30, 2025.")
    (chunk,) = chunk_document(make_doc(first, history))
    assert chunk.heading_path == TITLE
    assert body(chunk) == f"1. Policy\n\n{first.text}\n\nHistory\n\n{history.text}"


def test_short_section_that_would_exceed_max_stays_standalone() -> None:
    maker = TextMaker(8)
    params = {"target_tokens": 60, "max_tokens": 100, "overlap_tokens": 0, "min_tokens": 30}
    before = section_of_size(maker, (TITLE, "1. Before"), 85, 100)
    short = Section(
        (TITLE, "2. Note"),
        "See the appendix for the full list of exceptions that apply to this rule today.",
    )
    after = section_of_size(maker, (TITLE, "3. After"), 85, 100)
    chunks = chunk_document(make_doc(before, short, after), **params)
    assert [c.heading_path for c in chunks] == [
        f"{TITLE} > 1. Before",
        f"{TITLE} > 2. Note",
        f"{TITLE} > 3. After",
    ]
    assert chunks[1].token_count < 30
    assert all(c.token_count <= 100 for c in chunks)


def test_short_sections_keep_merging_until_none_fit() -> None:
    maker = TextMaker(9)
    params = {"target_tokens": 60, "max_tokens": 100, "overlap_tokens": 0, "min_tokens": 30}
    sections = [Section((TITLE, f"{i}. Item"), maker.sentence((3, 5))) for i in range(12)]
    chunks = chunk_document(make_doc(*sections), **params)
    assert all(c.token_count <= 100 for c in chunks)
    assert all(c.token_count >= 30 for c in chunks)
    everything = "\n".join(c.content for c in chunks)
    for section in sections:
        assert section.text in everything
        assert section.heading_path[1] in everything


def test_short_trailing_piece_rejoins_previous_piece() -> None:
    params = {"target_tokens": 60, "max_tokens": 200, "overlap_tokens": 0, "min_tokens": 30}
    paragraphs = [
        " ".join(f"Rule {p}.{n} applies to the office." for n in range(4)) for p in range(7)
    ]
    text = "\n\n".join([*paragraphs, "End."])
    chunks = chunk_document(make_doc(Section((TITLE, "1. Rules"), text)), **params)
    assert count_tokens(text) > 200
    assert all(count_tokens(paragraph) <= 60 - 12 for paragraph in paragraphs)
    assert len(chunks) == len(paragraphs)
    assert body(chunks[-1]) == f"{paragraphs[-1]}\n\nEnd."
    assert all(c.token_count <= 200 for c in chunks)


def test_merged_chunks_stay_within_target() -> None:
    maker = TextMaker(6)
    sections = [Section((TITLE, f"{i}. Item"), maker.paragraph(2)) for i in range(40)]
    chunks = chunk_document(make_doc(*sections), target_tokens=150, max_tokens=300, min_tokens=60)
    assert 1 < len(chunks) < 40
    assert all(c.token_count <= 150 for c in chunks)
    everything = "\n".join(c.content for c in chunks)
    for i in range(40):
        assert f"{i}. Item" in everything


def test_long_section_splits_at_paragraphs_with_overlap() -> None:
    maker = TextMaker(7)
    paragraphs = [maker.paragraph(4) for _ in range(30)]
    doc = make_doc(Section((TITLE, "4. Procedures"), "\n\n".join(paragraphs)))
    chunks = chunk_document(doc)

    assert len(chunks) >= 3
    for chunk in chunks:
        assert chunk.heading_path == f"{TITLE} > 4. Procedures"
        assert chunk.token_count <= 800
    for paragraph in paragraphs:  # paragraphs that fit are never split
        assert any(paragraph in c.content for c in chunks)
    for previous, current in zip(chunks, chunks[1:], strict=False):
        first_sentence = body(current).split(". ")[0] + "."
        assert first_sentence in body(previous), "each later piece starts with overlap"
        overlap = body(previous)[body(previous).index(first_sentence) :]
        assert 0 < count_tokens(overlap) <= 50


def test_giant_paragraph_splits_at_sentences() -> None:
    maker = TextMaker(8)
    paragraph = maker.paragraph(300)
    chunks = chunk_document(make_doc(Section((TITLE, "5. Rules"), paragraph)))
    assert count_tokens(paragraph) > 2000
    assert len(chunks) >= 5
    assert all(c.token_count <= 800 for c in chunks)
    everything = "\n".join(c.content for c in chunks)
    for sentence in maker.sentences:
        assert sentence in everything
    for chunk in chunks:  # pieces end at sentence boundaries
        assert body(chunk).endswith(".")


def test_giant_sentence_splits_by_words() -> None:
    words = [f"w{i}" for i in range(3000)]
    chunks = chunk_document(make_doc(Section((TITLE, "6. List"), " ".join(words))))
    assert len(chunks) >= 3
    assert all(c.token_count <= 800 for c in chunks)
    found = set(" ".join(body(c) for c in chunks).split())
    assert found == set(words)


def test_giant_word_splits_by_characters() -> None:
    word = "x" * 20000
    chunks = chunk_document(make_doc(Section((TITLE,), word)), target_tokens=100, max_tokens=150)
    assert all(c.token_count <= 150 for c in chunks)
    assert "".join(body(c).replace(ELLIPSIS, "") for c in chunks).count("x") >= 20000


def test_absurd_heading_is_truncated() -> None:
    title = " ".join(f"heading{i}" for i in range(2000))
    maker = TextMaker(9)
    doc = make_doc(Section((title, "1. Part"), maker.section_text(10, 6)), title=title)
    chunks = chunk_document(doc)
    for chunk in chunks:
        assert chunk.heading_path.endswith(ELLIPSIS)
        assert count_tokens(chunk.heading_path) <= 200
        assert chunk.token_count <= 800
    everything = "\n".join(c.content for c in chunks)
    assert all(sentence in everything for sentence in maker.sentences)


def test_empty_sections_and_documents_produce_no_chunks() -> None:
    assert chunk_document(make_doc()) == []
    assert chunk_document(make_doc(Section((TITLE, "1. Empty"), "  \n\n "))) == []


def test_missing_heading_path_uses_title() -> None:
    (chunk,) = chunk_document(make_doc(Section((), "Some text here.")))
    assert chunk.heading_path == TITLE


@pytest.mark.parametrize(
    "params",
    [
        {"target_tokens": 0},
        {"target_tokens": 900, "max_tokens": 800},
        {"min_tokens": 500},
        {"overlap_tokens": -1},
    ],
)
def test_invalid_parameters_raise(params: dict) -> None:
    with pytest.raises(ValueError):
        chunk_document(make_doc(), **params)
