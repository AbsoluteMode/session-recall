# tests/test_health.py
import time

from session_recall.health import (score, check_freshness, check_corpus,
                                   check_paths, check_secrets)
from session_recall.models import Chunk
from session_recall.store import Store


def _chunk(uuid, ts, session_id="s1", source="claude"):
    return Chunk(session_id=session_id, uuid=uuid, role="user", text=uuid, project="p",
                 cwd="/c", git_branch="b", ts=ts, file_path="/f.jsonl", byte_offset=0,
                 byte_len=5, turn_index=0, content_hash=uuid, source=source)


def test_score_bands_and_direction():
    """One scoring rule for every dimension, and it has to work in both directions:
    more sessions is better, more hours since the last index is worse."""
    assert score(100, green=50, amber=10).zone == "GREEN"
    assert score(20, green=50, amber=10).zone == "AMBER"
    assert score(2, green=50, amber=10).zone == "RED"
    assert score(1, green=24, amber=72, higher_is_better=False).zone == "GREEN"
    assert score(100, green=24, amber=72, higher_is_better=False).zone == "RED"


def test_secrets_dimension_is_absent_before_there_is_a_key(tmp_path):
    """A machine that never joined a hub has nothing to protect; an empty row
    would be noise in the one place that must stay scannable."""
    assert check_secrets((tmp_path / "hub.json",)) is None


def test_secrets_dimension_names_the_file_that_leaks(tmp_path, monkeypatch):
    """Nothing else in the tool would ever tell the user their key is readable,
    which is why the answer belongs in `health` rather than in a comment."""
    key = tmp_path / "hub.json"
    key.write_text('{"key": "sr_egor_deadbeef"}', encoding="utf-8")
    monkeypatch.setattr("session_recall.perms.exposure",
                        lambda p, *a, **kw: "mode 0644 — group or other can read it")
    dim = check_secrets((key,))
    assert dim.zone == "RED" and "hub.json" in dim.detail and dim.hint


def test_freshness_measures_the_gap_to_disk_not_the_index_alone(tmp_path):
    """The failure that went unnoticed for a day and a half: the index kept answering
    happily while transcripts on disk moved on without it. An index-only timestamp
    cannot see that — the gap between newest-on-disk and newest-indexed can."""
    store = Store(tmp_path / "h.db")
    old = int(time.time()) - 3 * 86400
    store.add(_chunk("u1", old), [0.0] * 1024)
    store.db.commit()

    transcript = tmp_path / "live.jsonl"
    transcript.write_text("{}\n")  # written now, three days after the newest chunk

    dim = check_freshness(store, [transcript])
    assert dim.zone == "RED", "a three-day gap must not read as healthy"
    assert "day" in dim.detail or "hour" in dim.detail, "detail must state the actual lag"
    assert dim.hint, "every failing dimension must say what to do about it"
    store.close()


def test_freshness_is_green_when_the_index_has_caught_up(tmp_path):
    store = Store(tmp_path / "h2.db")
    store.add(_chunk("u1", int(time.time())), [0.0] * 1024)
    store.db.commit()
    transcript = tmp_path / "live.jsonl"
    transcript.write_text("{}\n")
    assert check_freshness(store, [transcript]).zone == "GREEN"
    store.close()


def test_freshness_accepts_cursor_activity_without_jsonl_transcripts(tmp_path):
    """A Cursor-only install has no Claude/Codex JSONL tree to inspect."""
    store = Store(tmp_path / "cursor-only.db")
    now = int(time.time())
    store.add(_chunk("u1", now, source="cursor"), [0.0] * 1024)
    store.db.commit()

    dim = check_freshness(store, [], (now,))

    assert dim.zone == "GREEN"
    assert dim.detail == "up to date"
    store.close()


def test_corpus_counts_sessions_per_engine(tmp_path):
    """A total hides the failure worth catching: one source silently stopping."""
    store = Store(tmp_path / "c.db")
    now = int(time.time())
    store.add(_chunk("u1", now, session_id="s1"), [0.0] * 1024)
    store.add(_chunk("u2", now, session_id="s2", source="codex"), [0.0] * 1024)
    store.db.commit()

    dim = check_corpus(store)
    assert "claude" in dim.detail and "codex" in dim.detail
    store.close()


def test_paths_reports_which_sources_are_missing(tmp_path):
    """A wrong CODEX_HOME indexes nothing and looks exactly like having no Codex history."""
    present = tmp_path / "claude"
    present.mkdir()
    dim = check_paths({"claude": present, "codex": tmp_path / "nope"})
    assert dim.zone != "GREEN"
    assert "codex" in dim.detail, "the missing source must be named"


def test_embedder_check_surfaces_the_real_error(tmp_path):
    """Yesterday's failure verbatim: the provider answered 403 and the tool reported
    nothing. The check must quote what actually came back — 'unavailable' would have
    sent us to look at the key, which was fine."""
    from session_recall.health import check_embedder

    class _Down:
        def embed_query(self, text):
            raise RuntimeError("HTTP code 403 from API")

    dim = check_embedder(_Down())
    assert dim.zone == "RED"
    assert "403" in dim.detail, "the provider's own words, not a generic label"
    assert dim.hint


def test_embedder_check_is_green_when_it_answers():
    from session_recall.embed import FakeEmbedder
    from session_recall.health import check_embedder
    assert check_embedder(FakeEmbedder()).zone == "GREEN"


def test_vector_space_check_catches_same_dimension_model_swap(tmp_path, monkeypatch):
    from session_recall import config
    from session_recall.health import check_embed_space

    store = Store(tmp_path / "space.db")
    store.set_meta("embed_fp", "builtin/old-model/384")
    store.commit()
    monkeypatch.setattr(config, "EMBED_MODEL", "new-model")

    dim = check_embed_space(store)
    assert dim.zone == "RED"
    assert "old-model" in dim.detail and "new-model" in dim.detail
    assert "index" in dim.hint
    store.close()


def test_check_all_reports_every_dimension_and_a_verdict(tmp_path):
    """`health` has to answer one question first — is it working — and only then
    explain. A list of rows without a verdict makes the user do the aggregation."""
    from session_recall.embed import FakeEmbedder
    from session_recall.health import check_all

    store = Store(tmp_path / "all.db")
    store.add(_chunk("u1", int(time.time())), [0.0] * 1024)
    store.db.commit()
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n")

    report = check_all(store, FakeEmbedder(), {"claude": tmp_path}, [transcript])
    names = {d.name for d in report.dimensions}
    assert {"Freshness", "Corpus", "Sources", "Embedder"} <= names
    assert report.verdict in {"GREEN", "AMBER", "RED"}
    assert report.verdict == "RED" if any(
        d.zone == "RED" for d in report.dimensions) else True, "worst zone wins"
    store.close()
