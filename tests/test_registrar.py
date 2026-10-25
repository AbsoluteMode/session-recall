"""Recall as the memory of a "registrar": an agent that only answers questions
like "what is happening in project X", "who is working on what", "which
project does this belong to". Scenarios are built from real shapes seen in
the history: a session launched in one repo's worktree that edits another
repo, a symlinked alias, desktop side sessions, Codex threads opened by
Claude, a compacted summary as the best hit."""

import json
import os
from pathlib import Path

import pytest

from session_recall.embed import FakeEmbedder
from session_recall.index import index_corpus
from session_recall.repos import MIN_TOUCHES, RepoResolver, classify_starter
from session_recall.rerank import FakeReranker
from session_recall.retrieve import Recall, diversify
from session_recall.snippets import human_ask, query_snippet
from session_recall.store import Store
import session_recall.server as server

T0 = 1792900000  # 2026-10-25


def _iso(ts: int) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def user(sid, uuid, cwd, ts, text, entrypoint="claude-desktop", branch="main"):
    return {"type": "user", "sessionId": sid, "uuid": uuid, "cwd": cwd,
            "gitBranch": branch, "timestamp": _iso(ts), "entrypoint": entrypoint,
            "message": {"role": "user", "content": text}}


def assistant(sid, uuid, cwd, ts, text="", tools=(), branch="main"):
    content = [{"type": "text", "text": text}] if text else []
    content += [{"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}
                for i, (name, inp) in enumerate(tools)]
    return {"type": "assistant", "sessionId": sid, "uuid": uuid, "cwd": cwd,
            "gitBranch": branch, "timestamp": _iso(ts),
            "message": {"role": "assistant", "content": content}}


def write_session(projects: Path, name: str, lines: list[dict]) -> Path:
    folder = projects / f"-proj-{name}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


@pytest.fixture
def home(tmp_path):
    """A home with real git checkouts: two repos, a worktree of one, a
    symlinked alias, a plugin cache that is itself a git clone, and a folder
    that is not a repo at all."""
    h = tmp_path / "home"
    for repo in ("food", "harmonia", "whytap", "neighbour"):
        (h / repo / ".git").mkdir(parents=True)
    wt = h / "food" / ".claude" / "worktrees" / "wt"
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {h / 'food' / '.git' / 'worktrees' / 'wt'}\n")
    os.symlink(h / "whytap", h / "exodia")
    (h / ".claude" / "plugins" / "cache" / "tool" / ".git").mkdir(parents=True)
    (h / "notes").mkdir()
    cx = h / ".codex" / "worktrees" / "1a2b" / "whytap"
    cx.mkdir(parents=True)
    (cx / ".git").write_text(f"gitdir: {h / 'whytap' / '.git' / 'worktrees' / '1a2b'}\n")
    return h


def _index(tmp_path, home, embedder=None):
    store = Store(tmp_path / "r.db")
    index_corpus(store, embedder or FakeEmbedder(), tmp_path / "projects",
                 resolver=RepoResolver(home=str(home)))
    return store


def _recall(store):
    return Recall(store, FakeEmbedder(), FakeReranker())


def _cross_repo_session(home, sid="cross", ts=T0):
    """Launched in food's worktree, all real work done in ~/harmonia."""
    wt = str(home / "food" / ".claude" / "worktrees" / "wt")
    hm = home / "harmonia"
    edits = [("Edit", {"file_path": str(hm / "src" / f"f{i}.ts")}) for i in range(4)]
    return [
        user(sid, f"{sid}-u1", wt, ts, "сделай память двух уровней для оркестратора"),
        assistant(sid, f"{sid}-a1", wt, ts + 10, "Берусь за память оркестратора.",
                  tools=edits + [("Bash", {"command": f"cd {hm} && npm test"})]),
        assistant(sid, f"{sid}-a2", wt, ts + 20,
                  "Готово: память двух уровней в оркестраторе harmonia работает."),
    ]


# --- 1. which repo a session belongs to ------------------------------------

def test_session_is_attributed_to_the_repo_it_edited_not_its_launch_folder(tmp_path, home):
    write_session(tmp_path / "projects", "cross", _cross_repo_session(home))
    store = _index(tmp_path, home)
    repos = store.session_repos([("claude", "cross")])[("claude", "cross")]
    assert [r["repo"] for r in repos] == ["harmonia", "food"]
    assert repos[0]["path"] == os.path.realpath(home / "harmonia")
    assert repos[0]["touches"] == 5  # four Edits + one cd, each call counted once


def test_project_filter_finds_cross_repo_work_in_every_tool(tmp_path, home):
    write_session(tmp_path / "projects", "cross", _cross_repo_session(home))
    recall = _recall(_index(tmp_path, home))

    hits = recall.recall_search("память оркестратора", project="harmonia")
    assert hits and {h.session_id for h in hits} == {"cross"}
    assert hits[0].project == "food"           # where it was launched
    assert hits[0].repos == ["harmonia", "food"]  # what it worked on

    assert recall.recall_search("память оркестратора", project="neighbour") == []
    assert recall.grep("двух уровней", project="harmonia")
    assert recall.grep("двух уровней", project="neighbour") == []
    recent = recall.recent_sessions(project="HARMONIA", now=T0 + 30)  # case-insensitive
    assert [s["session_id"] for s in recent] == ["cross"]


def test_scope_cwd_of_the_edited_repo_includes_work_done_from_elsewhere(tmp_path, home):
    """The live failure: scope_cwd=~/harmonia returned only unrelated hits,
    because the work happened in a session launched elsewhere."""
    write_session(tmp_path / "projects", "cross", _cross_repo_session(home))
    recall = _recall(_index(tmp_path, home))
    scoped = recall.recall_search("память оркестратора", scope_cwd=str(home / "harmonia"))
    assert {h.session_id for h in scoped} == {"cross"}
    assert recall.grep("двух уровней", scope_cwd=str(home / "harmonia" / "src"))
    # The old cwd scope still works too.
    assert recall.recall_search("память оркестратора", scope_cwd=str(home / "food"))


def test_symlinked_alias_is_one_repo(tmp_path, home):
    alias = str(home / "exodia")
    write_session(tmp_path / "projects", "alias", [
        user("alias", "al-u", alias, T0, "добавь Meeting Assistant"),
        assistant("alias", "al-a", alias, T0 + 5, "Meeting Assistant подключён.",
                  tools=[("Bash", {"command": "swift build"})]),
    ])
    recall = _recall(_index(tmp_path, home))
    assert recall.recall_search("Meeting Assistant", project="whytap")[0].repos == ["whytap"]
    # The launch-folder name keeps working for anyone who knows it by that name.
    assert recall.recall_search("Meeting Assistant", project="exodia")
    assert recall.recall_search("Meeting Assistant", scope_cwd=str(home / "whytap"))


def test_a_glance_or_a_plugin_cache_is_not_work_on_a_repo(tmp_path, home):
    plugin = home / ".claude" / "plugins" / "cache" / "tool" / "SKILL.md"
    glance = [("Read", {"file_path": str(home / "neighbour" / "README.md")})]
    reads = [("Read", {"file_path": str(plugin)})] * 5
    tmp = [("Bash", {"command": "ls /tmp/x /private/var/folders/y"})] * 5
    write_session(tmp_path / "projects", "glance", [
        user("glance", "g-u", str(home / "food"), T0, "посмотри"),
        assistant("glance", "g-a", str(home / "food"), T0 + 1, "смотрю",
                  tools=glance * (MIN_TOUCHES - 1) + reads + tmp),
    ])
    store = _index(tmp_path, home)
    repos = store.session_repos([("claude", "glance")])[("claude", "glance")]
    assert [r["repo"] for r in repos] == ["food"]


def test_urls_and_odd_paths_never_hang_or_attribute(home):
    """Live hang: "https://github.com/x" yields "//github.com/x", and POSIX
    dirname("//") == "//" — the walk to the root never ended."""
    resolver = RepoResolver(home=str(home))
    for odd in ("//github.com/AbsoluteMode/harmonia", "//", "/", "~", "relative/x",
                "", "/nonexistent/deep/path/file.py"):
        assert resolver.repo_of(odd) is None
    from session_recall.repos import owning_repo
    assert owning_repo("//github.com/x") == os.path.realpath("//github.com/x")
    assert owning_repo(str(home / "harmonia" / "src")) == os.path.realpath(home / "harmonia")


def test_codex_worktree_in_a_hidden_folder_belongs_to_its_main_checkout(tmp_path, home):
    """Live gap: Codex keeps worktrees under ~/.codex/worktrees/<id>/<repo>;
    44 whytap sessions had no attribution while hidden folders were skipped."""
    cwd = str(home / ".codex" / "worktrees" / "1a2b" / "whytap")
    write_session(tmp_path / "projects", "cxwt", [
        user("cxwt", "cw-u", cwd, T0, "почини таймер"),
        assistant("cxwt", "cw-a", cwd, T0 + 1, "починил",
                  tools=[("Edit", {"file_path": cwd + "/Sources/Timer.swift"})]),
    ])
    store = _index(tmp_path, home)
    assert [r["repo"] for r in store.session_repos(
        [("claude", "cxwt")])[("claude", "cxwt")]] == ["whytap"]


def test_deleted_codex_worktree_falls_back_to_the_same_named_checkout(tmp_path, home):
    gone = str(home / ".codex" / "worktrees" / "9z9z" / "exodia")  # alias, deleted
    stray = str(home / ".codex" / "worktrees" / "8y8y" / "no-such-repo")
    write_session(tmp_path / "projects", "gonecx", [
        user("gonecx", "gc-u", gone, T0, "бэкап конфигов"),
        assistant("gonecx", "gc-a", gone, T0 + 1, "ок",
                  tools=[("Edit", {"file_path": gone + "/a.swift"}),
                         ("Read", {"file_path": stray + "/b.py"})] * MIN_TOUCHES),
    ])
    store = _index(tmp_path, home)
    assert [r["repo"] for r in store.session_repos(
        [("claude", "gonecx")])[("claude", "gonecx")]] == ["whytap"]


def test_deleted_worktree_and_non_repo_folder_still_attribute(tmp_path, home):
    gone = str(home / "food" / ".claude" / "worktrees" / "deleted-long-ago")
    notes = str(home / "notes")
    write_session(tmp_path / "projects", "gone", [
        user("gone", "go-u", gone, T0, "старая ветка"),
        assistant("gone", "go-a", gone, T0 + 1, "ок",
                  tools=[("Edit", {"file_path": gone + "/src/a.py"})]),
    ])
    write_session(tmp_path / "projects", "chat", [
        user("chat", "ch-u", notes, T0, "просто поговорить"),
        assistant("chat", "ch-a", notes, T0 + 1, "давай"),
    ])
    store = _index(tmp_path, home)
    facts = store.session_repos([("claude", "gone"), ("claude", "chat")])
    assert [r["repo"] for r in facts[("claude", "gone")]] == ["food"]
    # A pure discussion in a non-repo folder belongs to that folder.
    assert facts[("claude", "chat")] == [
        {"repo": "notes", "path": notes, "touches": 0}]


def test_a_scratch_session_in_hidden_or_library_folder_has_only_real_repos(tmp_path, home):
    scratch = str(home / "Library" / "Application Support" / "Claude" / "scratch-1")
    hm = str(home / "harmonia")
    write_session(tmp_path / "projects", "scratch", [
        user("scratch", "sc-u", scratch, T0, "вспомни harmonia"),
        assistant("scratch", "sc-a", scratch, T0 + 1, "вспомнил",
                  tools=[("Bash", {"command": f"git -C {hm} log"})] * 3),
    ])
    store = _index(tmp_path, home)
    assert [r["repo"] for r in store.session_repos(
        [("claude", "scratch")])[("claude", "scratch")]] == ["harmonia"]


def test_codex_workdir_and_exec_code_paths_are_attributed(tmp_path, home):
    codex_root = tmp_path / "codex"
    path = codex_root / "2026" / "10" / "25" / "rollout-x-01a1.jsonl"
    path.parent.mkdir(parents=True)
    hm, food = str(home / "harmonia"), str(home / "food")
    rows = [
        {"timestamp": _iso(T0), "type": "session_meta", "payload": {
            "id": "01a1", "cwd": food, "originator": "Claude Code", "source": "vscode"}},
        {"timestamp": _iso(T0 + 1), "type": "event_msg", "payload": {
            "type": "user_message", "message": "почини тесты harmonia"}},
    ] + [
        {"timestamp": _iso(T0 + 2 + i), "type": "response_item", "payload": {
            "type": "function_call", "name": "exec_command", "call_id": f"c{i}",
            "arguments": json.dumps({"cmd": "npm test", "workdir": hm})}}
        for i in range(2)
    ] + [
        {"timestamp": _iso(T0 + 5), "type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "exec", "call_id": "c9",
            "input": f'await tools.exec_command({{cmd:"cat {hm}/package.json"}})'}},
        {"timestamp": _iso(T0 + 6), "type": "event_msg", "payload": {
            "type": "agent_message", "message": "Тесты harmonia зелёные."}},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    store = Store(tmp_path / "r.db")
    index_corpus(store, FakeEmbedder(), None, (codex_root,),
                 resolver=RepoResolver(home=str(home)))
    recent = _recall(store).recent_sessions(now=T0 + 60)
    assert recent[0]["repos"][0] == {"repo": "harmonia", "path": hm, "touches": 3}
    assert recent[0]["started_by"] == "agent"  # a Codex thread Claude opened
    assert recent[0]["source"] == "codex"


# --- index lifecycle --------------------------------------------------------

class CountingEmbedder(FakeEmbedder):
    def __init__(self):
        super().__init__()
        self.documents = 0

    def embed_documents(self, texts):
        self.documents += len(texts)
        return super().embed_documents(texts)


def test_an_existing_index_is_backfilled_without_re_embedding(tmp_path, home):
    write_session(tmp_path / "projects", "cross", _cross_repo_session(home))
    embedder = CountingEmbedder()
    store = _index(tmp_path, home, embedder)
    embedded = embedder.documents
    # An index built before attribution existed: chunks, no facts.
    for table in ("session_repos", "session_info", "repo_scans"):
        store.db.execute(f"DELETE FROM {table}")
    store.commit()
    index_corpus(store, embedder, tmp_path / "projects",
                 resolver=RepoResolver(home=str(home)))
    assert embedder.documents == embedded
    assert store.session_repos([("claude", "cross")])[("claude", "cross")][0]["repo"] == "harmonia"
    # A further run with nothing new does no work at all.
    index_corpus(store, embedder, tmp_path / "projects",
                 resolver=RepoResolver(home=str(home)))
    assert store.db.execute("SELECT COUNT(*) FROM session_info").fetchone()[0] == 1


def test_growing_and_deleted_transcripts_keep_facts_in_step(tmp_path, home):
    lines = _cross_repo_session(home)
    path = write_session(tmp_path / "projects", "cross", lines)
    store = _index(tmp_path, home)
    nb = str(home / "neighbour")
    lines.append(assistant("cross", "cross-a3", lines[0]["cwd"], T0 + 40, "и сосед",
                           tools=[("Edit", {"file_path": nb + "/x.py"})] * MIN_TOUCHES))
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    os.utime(path, (T0 + 100, T0 + 100))
    index_corpus(store, FakeEmbedder(), tmp_path / "projects",
                 resolver=RepoResolver(home=str(home)))
    repos = store.session_repos([("claude", "cross")])[("claude", "cross")]
    assert [r["repo"] for r in repos] == ["harmonia", "neighbour", "food"]
    assert store.db.execute("SELECT COUNT(*) FROM session_info").fetchone()[0] == 1

    path.unlink()
    index_corpus(store, FakeEmbedder(), tmp_path / "projects",
                 resolver=RepoResolver(home=str(home)))
    for table in ("session_repos", "session_info", "repo_scans"):
        assert store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


# --- 2. who is doing what ---------------------------------------------------

def test_recent_sessions_reads_like_a_status_board(tmp_path, home):
    food = str(home / "food")
    write_session(tmp_path / "projects", "side", [
        user("side", "sd-u1", food, T0,
             "<system-reminder>worktree</system-reminder>"
             "Background (from the session that started this one):\nМаксим строит центр.\n\n"
             "Task:\nДоработай recall для регистратора", branch="claude/side"),
        assistant("side", "sd-a1", food, T0 + 5, "Начинаю с аудита.", branch="claude/side"),
        user("side", "sd-u2", food, T0 + 50,
             "<side-sessions-event>None of the sessions is working</side-sessions-event>",
             branch="claude/side"),
        user("side", "sd-u3", food, T0 + 60,
             '<pasted_content id="1">лог ошибки</pasted_content>\nпочини это',
             branch="claude/side"),
        assistant("side", "sd-a2", food, T0 + 70, "Починил, PR открыт.", branch="claude/side"),
    ])
    write_session(tmp_path / "projects", "sdk", [
        user("sdk", "sk-u", food, T0 - 5000, "summarize this", entrypoint="sdk-cli"),
        assistant("sdk", "sk-a", food, T0 - 4990, "summary"),
    ])
    recall = _recall(_index(tmp_path, home))
    board = {s["session_id"]: s for s in recall.recent_sessions(now=T0 + 100)}

    side = board["side"]
    assert side["label"] == "Доработай recall для регистратора"
    assert side["last_user"].startswith("лог ошибки")
    assert "почини это" in side["last_user"]
    assert side["last_assistant"] == "Починил, PR открыт."
    assert side["started_by"] == "agent"
    assert side["git_branch"] == "claude/side"
    assert side["active"] is True
    assert side["repos"][0]["repo"] == "food"

    assert board["sdk"]["started_by"] == "agent"
    assert board["sdk"]["active"] is False


def test_recent_sessions_label_falls_back_when_every_turn_is_machinery(tmp_path, home):
    food = str(home / "food")
    write_session(tmp_path / "projects", "beat", [
        user("beat", "b-u", food, T0, "<heartbeat><automation_id>w</automation_id></heartbeat>"),
        assistant("beat", "b-a", food, T0 + 1, "NOTIFY"),
    ])
    recall = _recall(_index(tmp_path, home))
    entry = recall.recent_sessions(now=T0 + 10)[0]
    assert entry["started_by"] == "automation"
    assert entry["label"].startswith("<heartbeat>")  # nothing better exists
    assert entry["last_user"] == ""


def test_classify_starter_edges():
    assert classify_starter("claude", "", "", "", "") == "user"
    assert classify_starter("claude", "cli", "", "", "  <heartbeat>x") == "automation"
    assert classify_starter("codex", "", "codex_exec", "exec", "do x") == "agent"
    assert classify_starter("codex", "", "Codex Desktop", "vscode", "do x") == "user"


# --- 3. diverse results -----------------------------------------------------

def test_one_long_session_does_not_take_every_slot(tmp_path, home):
    food = str(home / "food")
    long_lines = [user("long", "l-u", food, T0, "оркестратор")]
    long_lines += [assistant("long", f"l-a{i}", food, T0 + i, f"оркестратор шаг {i}")
                   for i in range(1, 7)]
    write_session(tmp_path / "projects", "long", long_lines)
    write_session(tmp_path / "projects", "other", [
        user("other", "o-u", food, T0, "оркестратор где мы"),
        assistant("other", "o-a", food, T0 + 1, "оркестратор: итог решения"),
    ])
    recall = _recall(_index(tmp_path, home))
    hits = recall.recall_search("оркестратор", k=4)
    by_session = [h.session_id for h in hits]
    assert by_session.count("long") == 2 and by_session.count("other") == 2
    uncapped = recall.recall_search("оркестратор", k=4, per_session=0)
    assert len(uncapped) == 4
    # Narrow history: fewer sessions than slots -> back-filled, never short.
    assert len(recall.recall_search("шаг", k=5)) == 5


def test_diversify_keeps_rank_order_and_backfills():
    items = [("a", 1), ("a", 2), ("a", 3), ("b", 4), ("a", 5)]
    assert diversify(items, 3, 2, key=lambda x: x[0]) == [("a", 1), ("a", 2), ("b", 4)]
    assert diversify(items, 4, 1, key=lambda x: x[0]) == [
        ("a", 1), ("a", 2), ("a", 3), ("b", 4)]
    assert diversify([], 3, 2, key=lambda x: x) == []
    assert diversify(items, 2, 0, key=lambda x: x[0]) == items[:2]


# --- 4. snippets around the query -------------------------------------------

def test_snippet_centres_on_the_query_and_drops_compaction_preamble():
    summary = ("This session is being continued from a previous conversation that ran "
               "out of context. The summary below covers the earlier portion of the "
               "conversation.\n\nSummary:\n1. Primary Request: " + "контекст " * 60
               + "решили оставить хаб выключенным до проверки ключа" + " хвост" * 40)
    snip = query_snippet(summary, "почему хаб выключен")
    assert "хаб выключенным" in snip
    assert "This session is being continued" not in snip
    assert snip.startswith("…") and snip.endswith("…")
    assert len(snip) <= 202


def test_snippet_without_literal_match_starts_at_the_beginning():
    text = "Primary answer first. " + "x" * 400
    assert query_snippet(text, "семантический запрос").startswith("Primary answer first.")
    assert query_snippet("short", "") == "short"
    assert query_snippet("", "q") == ""


def test_search_snippet_shows_the_matching_passage(tmp_path, home):
    food = str(home / "food")
    text = "Длинный отчёт. " + "детали " * 80 + "Ключ Voyage на сервере невалиден." + " ещё" * 50
    write_session(tmp_path / "projects", "rep", [
        user("rep", "r-u", food, T0, "что с хабом"),
        assistant("rep", "r-a", food, T0 + 1, text),
    ])
    recall = _recall(_index(tmp_path, home))
    hit = next(h for h in recall.recall_search("ключ Voyage невалиден", k=5)
               if h.uuid == "r-a")
    assert "Voyage" in hit.snippet


def test_human_ask_edges():
    assert human_ask("") is None
    assert human_ask("<side-sessions-event>x</side-sessions-event>") is None
    assert human_ask("<heartbeat>tick</heartbeat>") is None
    assert human_ask("This session is being continued from a previous conversation.\n"
                     "\nSummary:\nstuff") is None
    assert human_ask('<pasted_content id="9">текст</pasted_content>') == "текст"
    brief = "Background (from the session that started this one):\nфон\n\nTask:\nсделай X"
    assert human_ask(brief) == "сделай X"
    assert human_ask("Background (from the session that started this one): без задачи") \
        .startswith("Background")
    # An unclosed tag is the user quoting it, not machinery.
    assert human_ask("что значит <side-sessions-event> ?") == "что значит <side-sessions-event> ?"


# --- tool boundary ----------------------------------------------------------

def test_mcp_tools_forward_project_and_drop_empty_repos(tmp_path, home, monkeypatch):
    write_session(tmp_path / "projects", "cross", _cross_repo_session(home))
    monkeypatch.setattr(server, "_recall", _recall(_index(tmp_path, home)))
    anchors = server.recall_search("память оркестратора", project="harmonia")["anchors"]
    assert anchors and anchors[0]["repos"] == ["harmonia", "food"]
    assert server.grep("двух уровней", project="harmonia")
    assert server.recent_sessions(project="harmonia")[0]["session_id"] == "cross"
    from session_recall.models import Anchor
    bare = server._adict(Anchor(session_id="s", uuid="u", role="doc", snippet="",
                                score=None, project="p", when=0, source="metadocs"))
    assert "repos" not in bare


def test_hub_client_sends_project_to_the_server(monkeypatch):
    from session_recall.hub.client import HubConfig
    from session_recall.hub.remote import RemoteRecall
    sent = []
    remote = RemoteRecall(HubConfig(url="http://hub", key="k"))
    monkeypatch.setattr(remote, "_post", lambda path, body: sent.append((path, body)) or {})
    remote.recall_search("q", project="harmonia", per_session=0)
    remote.grep("p", project="harmonia")
    remote.recent_sessions(project="harmonia")
    assert [b["project"] for _, b in sent] == ["harmonia"] * 3
    assert sent[0][1]["per_session"] == 0
