import hashlib
import math
import os
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


def _fastembed_model(model_name: str):
    """Constructing the bundled model, isolated so tests can stand in for it
    without downloading anything."""
    import warnings
    from fastembed import TextEmbedding
    with warnings.catch_warnings():
        # fastembed warns that multilingual-MiniLM now uses mean pooling — a
        # migration note for pre-0.6 indexes. Our fingerprint was built WITH
        # mean pooling, so the note is pure noise on every process start.
        warnings.simplefilter("ignore", UserWarning)
        # fastembed's default cache is the system tmp dir, which macOS prunes —
        # the model would be re-downloaded after every cleanup. Keep it next
        # to the index instead.
        return TextEmbedding(model_name=model_name,
                             cache_dir=str(config.DATA_DIR / "models"))


class BuiltinEmbedder:
    """Bundled ONNX embeddings (fastembed) — the zero-setup default: no key,
    no server, CPU inference. The model is fetched from HuggingFace once on
    first use (70–220MB depending on language preset), then runs offline.
    Lazy like the API clients, so constructing costs nothing."""
    def __init__(self, model: str | None = None):
        self.model = model or config.EMBED_MODEL
        self._m = None

    @property
    def m(self):
        if self._m is None:
            self._m = _fastembed_model(self.model)
        return self._m

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[float(x) for x in v] for v in self.m.embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        # query_embed applies the model's query-side prompt where one exists
        # (bge instruction prefixes and the like) — .embed() would not
        return [float(x) for x in next(iter(self.m.query_embed(text)))]


def _openai_client(**kwargs):
    """Constructing the SDK, isolated so tests can stand in for it without a network."""
    from openai import OpenAI
    return OpenAI(**kwargs)


class OpenAIEmbedder:
    """OpenAI-compatible embeddings — OpenAI itself, or any server exposing
    /v1/embeddings (Ollama, LM Studio, llama.cpp, vLLM). Lazy client.

    `send_dimensions` exists because the parameter is not universal: OpenAI needs it so
    the vector matches the index, while local servers reject the request outright when
    it is present. Local servers also have no API key, but the SDK refuses to construct
    without one, hence the placeholder."""
    def __init__(self, model: str | None = None, dim: int | None = None,
                 base_url: str | None = None, send_dimensions: bool | None = None):
        self.model = model or config.EMBED_MODEL
        self.dim = dim or config.EMBED_DIM
        self.base_url = base_url if base_url is not None else config.EMBED_BASE_URL
        self.send_dimensions = (config.EMBED_SEND_DIMENSIONS
                                if send_dimensions is None else send_dimensions)
        self._client = None

    @property
    def client(self):
        if self._client is None:
            kwargs = {"api_key": os.environ.get("OPENAI_API_KEY") or "local-no-key"}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = _openai_client(**kwargs)
        return self._client

    def _embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), 128):
            kw = {"model": self.model, "input": texts[i:i + 128]}
            if self.dim and self.send_dimensions:
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
    if provider == "builtin":
        return BuiltinEmbedder(model=model)
    if provider == "fake":
        return FakeEmbedder(dim=dim)
    raise ValueError(f"unknown embed provider: {provider!r} (set SESSION_RECALL_EMBED_PROVIDER)")
