import pytest

from campus_rag.retrieve.fusion import reciprocal_rank_fusion
from campus_rag.retrieve.models import RetrievedChunk


def chunk(chunk_id: int, *, score: float = 0.0, content: str = "") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        document_id=chunk_id,
        url=f"https://example.test/{chunk_id}",
        title=f"Title {chunk_id}",
        heading_path=f"Heading {chunk_id}",
        content=content or f"Heading {chunk_id}\n\ntext {chunk_id}",
        token_count=10,
        score=score,
        rank=99,  # fusion must ignore the input rank
    )


def fuse(keyword: list[int], vector: list[int], **options: int) -> list[RetrievedChunk]:
    options.setdefault("limit", 10)
    return reciprocal_rank_fusion(
        keyword=[chunk(i) for i in keyword], vector=[chunk(i) for i in vector], **options
    )


def test_scores_are_sums_of_reciprocal_ranks() -> None:
    fused = fuse([1, 2], [2, 3], k=60)
    scores = {c.chunk_id: c.score for c in fused}
    assert scores[1] == pytest.approx(1 / 61)
    assert scores[2] == pytest.approx(1 / 62 + 1 / 61)
    assert scores[3] == pytest.approx(1 / 62)


def test_chunk_found_by_both_lists_outranks_chunks_found_by_one() -> None:
    fused = fuse([1, 2, 3], [4, 3, 5])
    assert [c.chunk_id for c in fused] == [3, 1, 4, 2, 5]
    assert [c.rank for c in fused] == [1, 2, 3, 4, 5]


def test_source_ranks_are_recorded_and_absent_ones_are_none() -> None:
    fused = {c.chunk_id: c for c in fuse([1, 2], [2, 3])}
    assert (fused[1].keyword_rank, fused[1].vector_rank) == (1, None)
    assert (fused[2].keyword_rank, fused[2].vector_rank) == (2, 1)
    assert (fused[3].keyword_rank, fused[3].vector_rank) == (None, 2)


def test_input_scores_and_ranks_are_ignored() -> None:
    keyword = [chunk(1, score=1000.0), chunk(2, score=0.001)]
    vector = [chunk(2, score=0.9), chunk(1, score=0.1)]
    fused = reciprocal_rank_fusion(keyword=keyword, vector=vector, limit=2)
    assert [c.score for c in fused] == pytest.approx([1 / 61 + 1 / 62] * 2)


def test_equal_scores_prefer_the_better_single_rank_then_the_lower_chunk_id() -> None:
    # Chunks 1 and 2 swap ranks across the lists, so their scores are equal; so are 3 and 4.
    fused = fuse([1, 2, 3, 4], [2, 1, 4, 3])
    assert [c.chunk_id for c in fused] == [1, 2, 3, 4]
    fused = fuse([5, 6], [6, 5])
    assert [c.chunk_id for c in fused] == [5, 6]
    fused = fuse([8, 7], [7, 8])
    assert [c.chunk_id for c in fused] == [7, 8]


def test_limit_truncates_and_ranks_are_renumbered() -> None:
    fused = fuse([1, 2, 3], [4, 5, 6], limit=4)
    assert len(fused) == 4
    assert [c.rank for c in fused] == [1, 2, 3, 4]


def test_empty_inputs() -> None:
    assert fuse([], []) == []
    only_vector = fuse([], [7, 8])
    assert [(c.chunk_id, c.keyword_rank, c.vector_rank) for c in only_vector] == [
        (7, None, 1),
        (8, None, 2),
    ]


def test_a_repeated_chunk_keeps_its_best_rank() -> None:
    fused = fuse([1, 2, 1], [])
    assert [c.chunk_id for c in fused] == [1, 2]
    assert fused[0].keyword_rank == 1 and fused[0].score == pytest.approx(1 / 61)


def test_k_sets_how_much_top_ranks_dominate() -> None:
    # Chunk 1 is first in one list only; chunk 2 is second in both.
    assert [c.chunk_id for c in fuse([1, 2], [3, 2], k=60)][0] == 2
    assert [c.chunk_id for c in fuse([1, 2], [3, 2], k=0)][0] == 1  # 1/1 ties 1/2 + 1/2


def test_other_fields_come_from_the_input_chunk() -> None:
    (fused,) = fuse([5], [], limit=1)
    assert (fused.url, fused.title, fused.token_count) == ("https://example.test/5", "Title 5", 10)


def test_invalid_arguments_are_rejected() -> None:
    with pytest.raises(ValueError, match="limit"):
        fuse([1], [2], limit=0)
    with pytest.raises(ValueError, match="k must"):
        fuse([1], [2], k=-1)
