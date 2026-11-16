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


class _VoyageStub:
    """Stands in for voyageai.Client with the real per-request token limit.
    `tokens_per_char` models how dense the text is; Voyage counts tokens, the
    indexer can only count characters."""
    LIMIT = 120_000

    def __init__(self, tokens_per_char=1.0):
        self.tokens_per_char = tokens_per_char
        self.calls = []

    def embed(self, batch, model, input_type):
        tokens = int(sum(len(t) for t in batch) * self.tokens_per_char)
        self.calls.append((len(batch), tokens))
        if len(batch) > 128:
            raise RuntimeError("batch size limit")
        if tokens > self.LIMIT:
            raise RuntimeError(
                f"Request to model 'voyage-4-large' failed. The max allowed tokens per "
                f"submitted batch is {self.LIMIT}. Your batch has {tokens} tokens after truncation.")
        return type("R", (), {"embeddings": [[float(len(t))] for t in batch]})()


def _voyage(stub):
    from session_recall.embed import VoyageEmbedder
    e = VoyageEmbedder(model="voyage-4-large")
    e._client = stub
    return e


def test_voyage_large_session_is_embedded_instead_of_failing_every_sync():
    """The real failure: 128 dense 2000-char chunks = 240k tokens in one
    request. Voyage refused it, the file was retried and refused on every sync,
    and the session never became searchable."""
    stub = _VoyageStub(tokens_per_char=0.94)
    texts = ["x" * (1900 + i) for i in range(300)]      # distinct lengths = identity
    vecs = _voyage(stub).embed_documents(texts)
    assert [v[0] for v in vecs] == [float(len(t)) for t in texts], "one vector per text, in order"
    assert all(tokens <= stub.LIMIT for _, tokens in stub.calls), "no request was refused"


def test_voyage_small_texts_still_go_128_per_request():
    stub = _VoyageStub()
    _voyage(stub).embed_documents(["short"] * 300)
    assert [n for n, _ in stub.calls] == [128, 128, 44], "no extra round trips for ordinary sessions"


def test_voyage_splits_when_the_character_estimate_is_too_optimistic():
    """Text denser than one token per character slips past the character
    budget; the API's refusal must split the batch, not fail the file."""
    stub = _VoyageStub(tokens_per_char=3.0)
    texts = ["y" * (1000 + i) for i in range(90)]        # ~95k chars, ~285k tokens
    vecs = _voyage(stub).embed_documents(texts)
    assert [v[0] for v in vecs] == [float(len(t)) for t in texts]
    assert any(tokens > stub.LIMIT for _, tokens in stub.calls), "the refusal path was exercised"


def test_voyage_oversized_single_text_and_empty_input():
    stub = _VoyageStub(tokens_per_char=0.5)
    huge = "z" * 150_000
    vecs = _voyage(stub).embed_documents(["a", huge, "b"])
    assert [v[0] for v in vecs] == [1.0, 150_000.0, 1.0]
    assert _voyage(_VoyageStub()).embed_documents([]) == []


def test_voyage_other_errors_are_not_swallowed_or_retried():
    import pytest

    class _Down:
        calls = 0
        def embed(self, batch, model, input_type):
            self.calls += 1
            raise RuntimeError("401 invalid api key")

    down = _Down()
    with pytest.raises(RuntimeError, match="401"):
        _voyage(down).embed_documents(["a", "b", "c"])
    assert down.calls == 1


def test_voyage_single_text_over_the_limit_surfaces_the_error():
    """Nothing left to split: the error must reach the indexer's failed list."""
    import pytest
    stub = _VoyageStub(tokens_per_char=3.0)
    with pytest.raises(RuntimeError, match="max allowed tokens"):
        _voyage(stub).embed_documents(["q" * 90_000])
    assert len(stub.calls) == 1
