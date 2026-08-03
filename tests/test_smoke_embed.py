"""Live smoke of the embedding pipeline: REAL bundled models, a real
sqlite-vec index, real semantic queries — no mocks anywhere in the chain.

Excluded from the default run (downloads models, CPU inference); run with
`pytest -m smoke`. The model cache is the real DATA_DIR/models — exactly the
production path — so repeat runs and a cached CI job skip the download.
"""

import json

import pytest

from session_recall import config
from session_recall import store as store_mod
from session_recall.embed import BuiltinEmbedder
from session_recall.index import index_corpus
from session_recall.retrieve import Recall
from session_recall.store import Store

pytestmark = pytest.mark.smoke


def _write_session(proj_dir, session_id, turns):
    lines = []
    for i, (role, text) in enumerate(turns):
        msg = ({"role": "user", "content": text} if role == "user" else
               {"role": "assistant", "content": [{"type": "text", "text": text}]})
        lines.append(json.dumps({
            "type": role, "uuid": f"{session_id}-{i}", "sessionId": session_id,
            "timestamp": f"2026-06-01T10:{i:02d}:00Z", "cwd": "/proj",
            "gitBranch": "main", "message": msg}))
    (proj_dir / f"{session_id}.jsonl").write_text("\n".join(lines))


def _pipeline(tmp_path, monkeypatch, model, dim, sessions):
    """The whole production chain with one substitution: synthetic transcripts."""
    monkeypatch.setattr(config, "EMBED_PROVIDER", "builtin")
    monkeypatch.setattr(config, "EMBED_MODEL", model)
    monkeypatch.setattr(config, "EMBED_DIM", dim)
    monkeypatch.setattr(store_mod, "EMBED_DIM", dim)   # from-import copy
    proj = tmp_path / "projects" / "-Users-me-proj"
    proj.mkdir(parents=True)
    for sid, turns in sessions.items():
        _write_session(proj, sid, turns)
    store = Store(tmp_path / "index.db")
    embedder = BuiltinEmbedder(model=model)
    assert index_corpus(store, embedder, tmp_path / "projects") > 0, \
        "the pipeline must produce chunks"
    return store, embedder


# Two arcs with zero word overlap against the queries below: a hit can only
# come from meaning, never from matching tokens.
AUTH = (
    ("user", "the login broke again — people get kicked out after five minutes"),
    ("assistant", "The OAuth refresh token was rotating on every request; I "
                  "pinned rotation to the session and expiry stopped. Deployed "
                  "and verified in production."),
)
PANTRY = (
    ("user", "plan the grocery list for the team offsite breakfast"),
    ("assistant", "Eggs, oat milk, rye bread and two kinds of jam — enough for "
                  "twelve people over three mornings."),
)


def test_en_specialist_ranks_by_meaning(tmp_path, monkeypatch):
    store, embedder = _pipeline(
        tmp_path, monkeypatch, "BAAI/bge-small-en-v1.5", 384,
        {"auth": AUTH, "pantry": PANTRY})
    try:
        hits = Recall(store, embedder).recall_search(
            "why were users being logged out so quickly", k=2)
        assert hits, "semantic search returned nothing"
        assert hits[0].score is not None, \
            "vector path must be live, not the FTS fallback"
        assert hits[0].session_id == "auth", "meaning must beat word overlap"
    finally:
        store.close()


def test_multilingual_model_crosses_languages(tmp_path, monkeypatch):
    """The builtin-multi promise in one assertion: a Russian question finds
    the English answer. This is what no lexical index can fake."""
    store, embedder = _pipeline(
        tmp_path, monkeypatch,
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", 384,
        {"auth": AUTH, "pantry": PANTRY})
    try:
        hits = Recall(store, embedder).recall_search(
            "почему пользователей выкидывало из аккаунта", k=2)
        assert hits and hits[0].score is not None
        assert hits[0].session_id == "auth", \
            "a Russian query must find the English fix"
    finally:
        store.close()
