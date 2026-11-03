import json
from pathlib import Path

import pytest

from campus_rag.evaluate.golden import (
    GoldenQuestion,
    GoldenSetError,
    GoldenTarget,
    chunk_matches,
    first_relevant_rank,
    load_golden_set,
)
from campus_rag.retrieve.models import RetrievedChunk

URL = "https://example.edu/policy/a/"


def entry(qid: str = "q001", **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": qid,
        "question": "Who approves this?",
        "type": "paraphrase",
        "expected": [{"url": URL, "heading_prefix": "Policy A > 1. Scope"}],
        "reference_answer": "The dean approves it.",
    }
    base.update(overrides)
    return base


def write_jsonl(tmp_path: Path, *entries: object) -> Path:
    path = tmp_path / "golden.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


def chunk(url: str, heading_path: str, rank: int = 1) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=rank,
        document_id=1,
        url=url,
        title="Policy A",
        heading_path=heading_path,
        content=heading_path,
        token_count=10,
        score=1.0,
        rank=rank,
    )


def test_load_valid_file(tmp_path):
    path = write_jsonl(
        tmp_path,
        entry("q001"),
        entry("q002", question="What about staff?", follow_up_of="q001", notes="follow-up"),
        entry("q003", type="unanswerable", expected=[], reference_answer="Not covered."),
    )
    questions = load_golden_set(path)
    assert [q.id for q in questions] == ["q001", "q002", "q003"]
    assert questions[0] == GoldenQuestion(
        id="q001",
        question="Who approves this?",
        type="paraphrase",
        expected=(GoldenTarget(URL, "Policy A > 1. Scope"),),
        reference_answer="The dean approves it.",
    )
    assert questions[1].follow_up_of == "q001"
    assert questions[1].notes == "follow-up"
    assert questions[2].expected == ()


def test_blank_lines_are_ignored(tmp_path):
    path = tmp_path / "golden.jsonl"
    path.write_text("\n" + json.dumps(entry()) + "\n\n", encoding="utf-8")
    assert len(load_golden_set(path)) == 1


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ([entry(), entry()], ":2 (q001): duplicate id 'q001'"),
        ([entry(type="trivia")], "type must be one of"),
        ([entry(question="  ")], "question must be a non-empty string"),
        ([entry(reference_answer="")], "reference_answer must be a non-empty string"),
        ([entry(type="unanswerable")], "unanswerable questions must have an empty expected"),
        ([entry(expected=[])], "need at least one expected target"),
        ([entry(expected="x")], "expected must be a list"),
        ([entry(expected=[{"url": URL}])], "exactly the fields url and heading_prefix"),
        ([entry(expected=[{"url": URL, "heading_prefix": ""}])], "heading_prefix must be"),
        ([entry(follow_up_of="q002"), entry("q002")], ":1 (q001): follow_up_of 'q002'"),
        ([entry(follow_up_of="q001")], "cannot follow up on itself"),
        ([entry(extra=1)], "unknown field(s): extra"),
        ([{"id": "q001"}], "missing required field(s): question, type"),
        (["not an object"], "must be a JSON object"),
    ],
)
def test_invalid_entries_are_rejected(tmp_path, entries, message):
    with pytest.raises(GoldenSetError) as excinfo:
        load_golden_set(write_jsonl(tmp_path, *entries))
    assert message in str(excinfo.value)


def test_invalid_json_reports_line_number(tmp_path):
    path = tmp_path / "golden.jsonl"
    path.write_text(json.dumps(entry()) + "\n{not json\n", encoding="utf-8")
    with pytest.raises(GoldenSetError, match=r"golden\.jsonl:2: invalid JSON"):
        load_golden_set(path)


def test_empty_and_missing_files_are_rejected(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n", encoding="utf-8")
    with pytest.raises(GoldenSetError, match="no questions"):
        load_golden_set(empty)
    with pytest.raises(GoldenSetError, match="cannot read file"):
        load_golden_set(tmp_path / "missing.jsonl")


@pytest.mark.parametrize(
    ("chunk_url", "chunk_heading", "target_heading", "expected"),
    [
        (URL, "Policy A > 1. Scope", "Policy A > 1. Scope", True),
        (URL, "Policy A > 1. Scope > B. Staff", "Policy A > 1. Scope", True),  # target is prefix
        (URL, "Policy A", "Policy A > 1. Scope", True),  # chunk is prefix
        (URL.rstrip("/"), "Policy A > 1. Scope", "Policy A > 1. Scope", True),
        (URL, "Policy A > 1. Scope", "Policy A > 1. Scop", False),  # not a whole segment
        (URL, "Policy A > 1. Scope 2", "Policy A > 1. Scope", False),
        (URL, "Policy A > 2. Rules", "Policy A > 1. Scope", False),
        ("https://example.edu/policy/b/", "Policy A > 1. Scope", "Policy A > 1. Scope", False),
    ],
)
def test_chunk_matches(chunk_url, chunk_heading, target_heading, expected):
    assert chunk_matches(chunk_url, chunk_heading, GoldenTarget(URL, target_heading)) is expected


def test_target_url_trailing_slash_is_ignored():
    target = GoldenTarget(URL.rstrip("/"), "Policy A")
    assert chunk_matches(URL, "Policy A > 1. Scope", target)


def test_first_relevant_rank():
    question = GoldenQuestion(
        id="q001",
        question="Who approves this?",
        type="paraphrase",
        expected=(
            GoldenTarget(URL, "Policy A > 3. Approval"),
            GoldenTarget("https://example.edu/policy/b/", "Policy B > 2. Review"),
        ),
        reference_answer="The dean.",
    )
    retrieved = [
        chunk(URL, "Policy A > 1. Scope", rank=1),
        chunk("https://example.edu/policy/b/", "Policy B > 2. Review > A. Steps", rank=2),
        chunk(URL, "Policy A > 3. Approval", rank=3),
    ]
    assert first_relevant_rank(question, retrieved) == 2
    assert first_relevant_rank(question, retrieved[:1]) is None
    assert first_relevant_rank(question, []) is None


def test_first_relevant_rank_is_none_for_unanswerable():
    question = GoldenQuestion(
        id="q001",
        question="When is spring break?",
        type="unanswerable",
        expected=(),
        reference_answer="Not covered.",
    )
    assert first_relevant_rank(question, [chunk(URL, "Policy A")]) is None


def test_repository_golden_set():
    questions = load_golden_set()
    counts = {t: sum(q.type == t for q in questions) for t in ("paraphrase", "exact_term")}
    unanswerable = [q for q in questions if q.type == "unanswerable"]
    assert 45 <= len(questions) <= 50
    assert counts["paraphrase"] >= 20
    assert counts["exact_term"] >= 13
    assert len(unanswerable) >= 6

    by_id = {q.id: q for q in questions}
    follow_ups = [q for q in questions if q.follow_up_of]
    assert 3 <= len(follow_ups) <= 4
    for question in follow_ups:
        assert by_id[question.follow_up_of].type != "unanswerable"
        assert question.expected
    for question in questions:
        assert all(t.url.startswith("https://policy.uw.edu/directory/") for t in question.expected)
