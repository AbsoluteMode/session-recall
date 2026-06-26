# tests/test_rerank.py
from session_recall.rerank import FakeReranker

def test_fake_ranks_by_overlap():
    r = FakeReranker()
    docs = ["totally unrelated text", "resilient drop delivery design", "drop delivery"]
    ranked = r.rerank("how does drop delivery work", docs, top_k=2)
    assert len(ranked) == 2
    # docs containing query terms rank above the unrelated one
    assert ranked[0][0] in (1, 2)
    assert ranked[0][1] >= ranked[1][1]      # scores sorted descending
    assert isinstance(ranked[0][1], float)


def test_make_reranker_dispatch():
    import pytest
    from session_recall.rerank import make_reranker, VoyageReranker, FakeReranker
    assert make_reranker("none") is None        # disabled -> None (graceful KNN+FTS fallback)
    assert make_reranker("off") is None
    assert isinstance(make_reranker("voyage"), VoyageReranker)
    assert isinstance(make_reranker("fake"), FakeReranker)
    with pytest.raises(ValueError):
        make_reranker("nope")
