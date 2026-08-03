from session_recall.embed import FakeEmbedder
from session_recall.config import EMBED_DIM

def test_fake_is_deterministic_and_right_dim():
    e = FakeEmbedder()
    v1 = e.embed_query("hello")
    v2 = e.embed_query("hello")
    assert v1 == v2 and len(v1) == EMBED_DIM
    assert e.embed_query("world") != v1

def test_fake_documents_batch():
    e = FakeEmbedder()
    vecs = e.embed_documents(["a", "b"])
    assert len(vecs) == 2 and all(len(v) == EMBED_DIM for v in vecs)


def test_make_embedder_dispatch_and_lazy_clients():
    import pytest
    from session_recall.embed import make_embedder, FakeEmbedder, VoyageEmbedder, OpenAIEmbedder
    assert isinstance(make_embedder("fake"), FakeEmbedder)
    # voyage/openai must CONSTRUCT without a key (client is lazy) -> factory-friendly + no network
    assert isinstance(make_embedder("voyage"), VoyageEmbedder)
    assert isinstance(make_embedder("openai"), OpenAIEmbedder)
    with pytest.raises(ValueError):
        make_embedder("nope")


def test_make_embedder_defaults_to_config(monkeypatch):
    from session_recall import config, embed
    monkeypatch.setattr(config, "EMBED_PROVIDER", "fake")
    assert isinstance(embed.make_embedder(), embed.FakeEmbedder)


class _RecordingClient:
    """Stands in for the OpenAI SDK: records how it was built and what it was asked."""
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        self.embeddings = self

    def create(self, **kw):
        self.calls.append(kw)
        return type("R", (), {"data": [type("D", (), {"embedding": [0.0] * 8})()
                                       for _ in kw["input"]]})()


def test_openai_embedder_omits_dimensions_for_servers_that_reject_it(monkeypatch):
    """Ollama and llama.cpp 400 on `dimensions`. Sending it unconditionally is what
    makes 'point it at your own endpoint' fail on every local server."""
    from session_recall import embed
    client = _RecordingClient()
    monkeypatch.setattr(embed, "_openai_client", lambda **kw: client)

    embed.OpenAIEmbedder(model="nomic-embed-text", dim=768,
                         send_dimensions=False).embed_query("hi")
    assert "dimensions" not in client.calls[0], "local endpoints reject the parameter"

    embed.OpenAIEmbedder(model="text-embedding-3-large", dim=1024,
                         send_dimensions=True).embed_query("hi")
    assert client.calls[1]["dimensions"] == 1024, "OpenAI needs it to match the index"


def test_openai_embedder_reaches_a_local_endpoint_without_a_key(monkeypatch):
    """A local server has no API key, but the SDK refuses to construct without one —
    so an unauthenticated endpoint needs a placeholder, not a crash."""
    from session_recall import embed
    seen = {}
    monkeypatch.setattr(embed, "_openai_client",
                        lambda **kw: seen.update(kw) or _RecordingClient())
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    embed.OpenAIEmbedder(base_url="http://127.0.0.1:11434/v1").embed_query("hi")
    assert seen["base_url"] == "http://127.0.0.1:11434/v1"
    assert seen["api_key"], "SDK requires a non-empty key even when the server ignores it"


class _RecordingModel:
    """Stands in for fastembed.TextEmbedding: no download, no ONNX runtime."""
    def __init__(self):
        self.doc_calls, self.query_calls = [], []

    def embed(self, texts):
        self.doc_calls.append(list(texts))
        return ([0.5] * 4 for _ in texts)          # generator, like the real one

    def query_embed(self, text):
        self.query_calls.append(text)
        yield [0.25] * 4


def test_builtin_embedder_is_lazy_and_uses_the_query_path(monkeypatch):
    """The bundled model must not download at construction time (the factory
    builds embedders unconditionally), and queries must go through
    query_embed — that is where models like bge apply their query prefix,
    which .embed() would silently skip."""
    from session_recall import embed
    made = []
    model = _RecordingModel()
    monkeypatch.setattr(embed, "_fastembed_model",
                        lambda name: made.append(name) or model)

    e = embed.BuiltinEmbedder(model="BAAI/bge-small-en-v1.5")
    assert made == [], "constructing must not touch the model"

    docs = e.embed_documents(["a", "b"])
    assert made == ["BAAI/bge-small-en-v1.5"]
    assert docs == [[0.5] * 4, [0.5] * 4] and isinstance(docs[0][0], float)

    q = e.embed_query("find it")
    assert model.query_calls == ["find it"] and q == [0.25] * 4


def test_make_embedder_knows_builtin(monkeypatch):
    from session_recall import embed
    assert isinstance(embed.make_embedder("builtin"), embed.BuiltinEmbedder)
