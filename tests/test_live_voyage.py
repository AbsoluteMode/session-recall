import os
import pytest
from session_recall.embed import VoyageEmbedder
from session_recall.rerank import VoyageReranker
from session_recall.config import EMBED_DIM


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("VOYAGE_API_KEY"), reason="no key")
def test_voyage_embed_and_rerank_live():
    v = VoyageEmbedder().embed_query("hello world")
    assert len(v) == EMBED_DIM
    ranked = VoyageReranker().rerank("drop delivery", ["drop delivery design", "pasta recipe"], top_k=1)
    assert ranked[0][0] == 0
