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


def test_codex_roots_follow_codex_home(monkeypatch, tmp_path):
    import importlib
    with monkeypatch.context() as scoped:
        scoped.setenv("CODEX_HOME", str(tmp_path / "custom-codex"))
        importlib.reload(config)
        assert config.CODEX_SESSIONS == tmp_path / "custom-codex" / "sessions"
        assert config.CODEX_ARCHIVED_SESSIONS == tmp_path / "custom-codex" / "archived_sessions"
    importlib.reload(config)

def test_cursor_db_follows_each_platform_own_app_data_dir():
    """Cursor stores its state where its VS Code base does. Reading `~/.config`
    on Windows found nothing and reported `sources: missing cursor` while the
    file sat in %APPDATA% the whole time."""
    mac = config._default_cursor_db("darwin", {})
    win = config._default_cursor_db("win32", {"APPDATA": r"C:\Users\egor\AppData\Roaming"})
    linux = config._default_cursor_db("linux", {"XDG_CONFIG_HOME": "/home/egor/.config"})

    assert mac.parts[-5:] == ("Application Support", "Cursor", "User",
                              "globalStorage", "state.vscdb")
    assert win == Path(r"C:\Users\egor\AppData\Roaming") / "Cursor" / "User" \
        / "globalStorage" / "state.vscdb"
    assert linux == Path("/home/egor/.config") / "Cursor" / "User" \
        / "globalStorage" / "state.vscdb"


def test_chunk_dataclass():
    c = Chunk(session_id="s", uuid="u", role="user", text="hi", project="p",
              cwd="/c", git_branch="b", ts=1, file_path="/f.jsonl",
              byte_offset=0, byte_len=10, turn_index=0, content_hash="h")
    assert c.role == "user" and c.byte_len == 10
