import shutil
import subprocess
import sys
from session_recall import cli, config
from session_recall.embed import FakeEmbedder


def test_cli_index_then_search(tmp_path, monkeypatch, capsys):
    proj = tmp_path / "projects" / "-Users-me-proj"
    proj.mkdir(parents=True)
    shutil.copy("tests/fixtures/session_a.jsonl", proj / "session_a.jsonl")
    monkeypatch.setattr(config, "CLAUDE_PROJECTS", tmp_path / "projects")
    monkeypatch.setattr(config, "CODEX_SESSIONS", tmp_path / "no-codex-sessions")
    monkeypatch.setattr(config, "CODEX_ARCHIVED_SESSIONS", tmp_path / "no-codex-archive")
    monkeypatch.setattr(config, "CURSOR_DB", tmp_path / "no-cursor.db")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "cli.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")  # keep the live metadocs config out of the test
    monkeypatch.setattr(cli, "make_embedder", lambda: FakeEmbedder())
    monkeypatch.setattr(cli, "make_reranker", lambda: __import__("session_recall.rerank", fromlist=["FakeReranker"]).FakeReranker())
    cli.main(["index"])
    cli.main(["search", "cache embeddings"])
    out = capsys.readouterr().out
    assert "cache" in out


def test_cli_recent_grep_prune(tmp_path, monkeypatch, capsys):
    """Debugging without MCP: `recent` lists sessions, `grep` scans raw
    transcripts, `prune` reports dropped deleted-file rows."""
    proj = tmp_path / "projects" / "-Users-me-proj"
    proj.mkdir(parents=True)
    shutil.copy("tests/fixtures/session_a.jsonl", proj / "session_a.jsonl")
    monkeypatch.setattr(config, "CLAUDE_PROJECTS", tmp_path / "projects")
    monkeypatch.setattr(config, "CODEX_SESSIONS", tmp_path / "no-codex-sessions")
    monkeypatch.setattr(config, "CODEX_ARCHIVED_SESSIONS", tmp_path / "no-codex-archive")
    monkeypatch.setattr(config, "CURSOR_DB", tmp_path / "no-cursor.db")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "cli.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")  # keep the live metadocs config out of the test
    monkeypatch.setattr(cli, "make_embedder", lambda: FakeEmbedder())
    monkeypatch.setattr(cli, "make_reranker", lambda: None)
    cli.main(["index"])
    capsys.readouterr()

    cli.main(["recent"])
    out = capsys.readouterr().out
    assert "sa" in out and "cache embeddings" in out  # session id + first-prompt label

    cli.main([
        "recent", "--start-date", "2026-06-02", "--end-date", "2026-06-02",
        "--timezone", "UTC",
    ])
    assert "sa" not in capsys.readouterr().out

    cli.main([
        "recent", "--date", "2026-06-01", "--timezone", "UTC",
    ])
    assert "sa" in capsys.readouterr().out

    cli.main(["grep", "tool output not human", "--limit", "1"])
    out = capsys.readouterr().out
    assert "u2" in out  # the tool_result turn's uuid

    cli.main(["prune"])
    out = capsys.readouterr().out
    assert "pruned 0" in out


def test_cli_cursor_schema_failure_keeps_other_sources(tmp_path, monkeypatch, capsys):
    """Cursor is a private schema. A breaking upgrade reports failure while
    Claude/Codex commits from the same run remain available."""
    proj = tmp_path / "projects" / "-Users-me-proj"
    proj.mkdir(parents=True)
    shutil.copy("tests/fixtures/session_a.jsonl", proj / "session_a.jsonl")
    cursor_db = tmp_path / "cursor.vscdb"
    import sqlite3
    sqlite3.connect(cursor_db).close()  # incompatible: no cursorDiskKV

    monkeypatch.setattr(config, "CLAUDE_PROJECTS", tmp_path / "projects")
    monkeypatch.setattr(config, "CODEX_SESSIONS", tmp_path / "no-codex-sessions")
    monkeypatch.setattr(config, "CODEX_ARCHIVED_SESSIONS", tmp_path / "no-codex-archive")
    monkeypatch.setattr(config, "CURSOR_DB", cursor_db)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "cli.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(cli, "make_embedder", lambda: FakeEmbedder())

    assert cli.main(["index"]) == 1
    captured = capsys.readouterr()
    assert "Cursor indexing failed" in captured.err

    from session_recall.store import Store
    store = Store(config.DB_PATH)
    assert store.db.execute(
        "SELECT COUNT(*) FROM chunks WHERE source='claude'").fetchone()[0] > 0
    store.close()


def test_cli_module_entrypoint_runs_main():
    # Regression: `python -m session_recall.cli` must invoke main(), not no-op.
    # A missing __main__ guard once made `index` silently do nothing (no DB).
    proc = subprocess.run(
        [sys.executable, "-m", "session_recall.cli"],
        capture_output=True,
        text=True,
    )
    # argparse requires a subcommand -> exit 2 + usage text proves main() ran.
    assert proc.returncode == 2
    assert "usage" in proc.stderr.lower()
