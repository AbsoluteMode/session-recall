from pathlib import Path
from session_recall import config
from session_recall.models import Chunk

def test_data_dir_outside_repo_under_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    import importlib; importlib.reload(config)
    assert config.DATA_DIR == tmp_path / "session-recall"
    assert config.DB_PATH == tmp_path / "session-recall" / "index.db"

def test_model_constants():
    assert config.EMBED_MODEL == "voyage-4-large"
    assert config.EMBED_DIM == 1024
    assert config.RERANK_MODEL == "rerank-2.5"

def test_chunk_dataclass():
    c = Chunk(session_id="s", uuid="u", role="user", text="hi", project="p",
              cwd="/c", git_branch="b", ts=1, file_path="/f.jsonl",
              byte_offset=0, byte_len=10, turn_index=0, content_hash="h")
    assert c.role == "user" and c.byte_len == 10
