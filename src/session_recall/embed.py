import hashlib
import math
from typing import Protocol
from . import config


class Embedder(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class FakeEmbedder:
    """Deterministic pseudo-embeddings from a text hash. No network — used in tests."""
    def __init__(self, dim: int | None = None):
        self.dim = dim or config.EMBED_DIM
        self.doc_calls = 0

    def _vec(self, text: str) -> list[float]:
        h = hashlib.sha256(text.encode("utf-8")).digest()
        seed = int.from_bytes(h[:8], "big")
        vals = []
        for _ in range(self.dim):
            seed = (seed * 6364136223846793005 + 1442695040888963407) & ((1 << 64) - 1)
            vals.append((seed >> 11) / float(1 << 53) * 2 - 1)
        norm = math.sqrt(sum(v * v for v in vals)) or 1.0
        return [v / norm for v in vals]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.doc_calls += 1
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


class VoyageEmbedder:
    """Voyage embeddings (default). Reads VOYAGE_API_KEY from env. The client is created
    lazily on first use, so constructing the embedder needs no key — the factory can
    build it without touching the network."""
    def __init__(self, model: str | None = None):
        self.model = model or config.EMBED_MODEL
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import voyageai
            self._client = voyageai.Client()
        return self._client

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), 128):
            batch = texts[i:i + 128]
            out.extend(self.client.embed(batch, model=self.model, input_type="document").embeddings)
        return out

    def embed_query(self, text: str) -> list[float]:
        return self.client.embed([text], model=self.model, input_type="query").embeddings[0]


class OpenAIEmbedder:
    """OpenAI-compatible embeddings — works with OpenAI and any provider exposing a
    /v1/embeddings endpoint (point OPENAI_BASE_URL at it). Reads OPENAI_API_KEY from env.
    Lazy client. `dim`, when set, is passed as the `dimensions` parameter (supported by
    text-embedding-3-* and compatible models) so the vector matches the index dimension."""
    def __init__(self, model: str | None = None, dim: int | None = None):
        self.model = model or config.EMBED_MODEL
        self.dim = dim or config.EMBED_DIM
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI()
        return self._client

    def _embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), 128):
            kw = {"model": self.model, "input": texts[i:i + 128]}
            if self.dim:
                kw["dimensions"] = self.dim
            out.extend(d.embedding for d in self.client.embeddings.create(**kw).data)
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text])[0]


def make_embedder(provider: str | None = None, model: str | None = None,
                  dim: int | None = None) -> Embedder:
    """Build the configured embedder. provider/model/dim default to config (env-driven,
    Voyage by default). One branch per provider — everything downstream is provider-agnostic."""
    provider = (provider or config.EMBED_PROVIDER).lower()
    if provider == "voyage":
        return VoyageEmbedder(model=model)
    if provider in ("openai", "openai-compatible"):
        return OpenAIEmbedder(model=model, dim=dim)
    if provider == "fake":
        return FakeEmbedder(dim=dim)
    raise ValueError(f"unknown embed provider: {provider!r} (set SESSION_RECALL_EMBED_PROVIDER)")
