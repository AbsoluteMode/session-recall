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
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "cli.db")
    monkeypatch.setattr(cli, "make_embedder", lambda: FakeEmbedder())
    monkeypatch.setattr(cli, "make_reranker", lambda: __import__("session_recall.rerank", fromlist=["FakeReranker"]).FakeReranker())
    cli.main(["index"])
    cli.main(["search", "cache embeddings"])
    out = capsys.readouterr().out
    assert "cache" in out


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
