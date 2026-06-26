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
