# src/session_recall/rerank.py
from typing import Optional, Protocol
from . import config


class Reranker(Protocol):
    def rerank(self, query: str, documents: list[str], top_k: int) -> list[tuple[int, float]]: ...


class FakeReranker:
    """Deterministic lexical-overlap scorer. No network — used in tests."""
    def rerank(self, query: str, documents: list[str], top_k: int) -> list[tuple[int, float]]:
        q = set(query.lower().split())
        scored = []
        for i, d in enumerate(documents):
            words = d.lower().split()
            overlap = sum(1 for w in words if w in q)
            score = overlap / (len(words) or 1)
            scored.append((i, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]


class VoyageReranker:
    """Voyage cross-encoder reranker (default). Lazy client (no key needed to construct)."""
    def __init__(self, model: str | None = None):
        self.model = model or config.RERANK_MODEL
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import voyageai
            self._client = voyageai.Client()
        return self._client

    def rerank(self, query: str, documents: list[str], top_k: int) -> list[tuple[int, float]]:
        if not documents:
            return []
        res = self.client.rerank(query, documents, model=self.model, top_k=top_k)
        results = [(r.index, r.relevance_score) for r in res.results]
        results.sort(key=lambda x: x[1], reverse=True)
        return results


def make_reranker(provider: str | None = None, model: str | None = None) -> Optional[Reranker]:
    """Build the configured reranker, or None to disable reranking (graceful KNN+FTS
    fallback). Set SESSION_RECALL_RERANK_PROVIDER=none for providers without a reranker."""
    provider = (provider or config.RERANK_PROVIDER or "none").lower()
    if provider in ("none", "off", ""):
        return None
    if provider == "voyage":
        return VoyageReranker(model=model)
    if provider == "fake":
        return FakeReranker()
    raise ValueError(f"unknown rerank provider: {provider!r} (set SESSION_RECALL_RERANK_PROVIDER)")
