"""`ask` and the MCP proxy: the two ways a person or an agent reads the
pooled index."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from session_recall.hub import ask as hub_ask
from session_recall.hub.app import Hub, make_handler
from session_recall.hub.client import HubConfig
from session_recall.hub.remote import RemoteRecall
from session_recall.models import Anchor, Turn
from session_recall.retrieve import SearchResult


class FakeStore:
    def __init__(self, rows):
        self.db = self
        self._rows = rows

    def execute(self, sql, params=()):
        return self

    def fetchall(self):
        return self._rows


class FakeRecall:
    def __init__(self, hub_root=""):
        self.store = FakeStore([
            ("s1", f"{hub_root}/transcripts/egor/claude/-Users-egor-proj/s1.jsonl")])
        self.searched = []

    def recall_search(self, query, **kw):
        self.searched.append((query, kw))
        return SearchResult([Anchor(
            session_id="s1", uuid="u1", role="assistant",
            snippet="запинили mcp<2 и перевыпустили конфиг", score=0.8,
            project="pr-review", when=1785000000, source="claude")])

    def expand_around(self, session_id, uuid, before=2, after=2, source=None):
        return [Turn(role="user", type="user", content="как чинили?", raw={})]

    def step(self, session_id, uuid, direction, count=1, source=None):
        return [Turn(role="assistant", type="assistant", content="вот так", raw={})]

    def grep(self, pattern, session_id=None, **kw):
        return [Anchor(session_id="s1", uuid="u9", role="user", snippet=pattern,
                       score=1.0, project="pr-review", when=1785000000)]

    def recent_sessions(self, **kw):
        return [{"source": "claude", "session_id": "s1", "project": "pr-review",
                 "turns": 12, "last_activity_human": "вчера", "label": "работа"}]


@pytest.fixture
def hub(tmp_path):
    root = tmp_path / "hub"
    # ONE instance shared across threads: the real Recall is thread-local (a
    # sqlite connection cannot be shared), so a per-thread factory would hand
    # the assertions in this file a different object than the request used.
    shared = FakeRecall(str(root))
    return Hub(root, recall_factory=lambda: shared, composer=None)


@pytest.fixture
def url(hub):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(hub))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def remote(url, hub):
    return RemoteRecall(HubConfig(url, hub.keys.issue("maxim")))


# -- ask ---------------------------------------------------------------------

def test_without_a_model_ask_still_answers_with_what_it_found(hub):
    result = hub_ask.answer(hub, "как чинили CI?", composer=None)
    assert result["composed"] is False
    assert "запинили mcp<2" in result["answer"]


def test_with_a_model_the_answer_is_written(hub):
    result = hub_ask.answer(hub, "как чинили CI?",
                            composer=lambda req, chunks: "Запинили mcp<2.")
    assert (result["composed"], result["answer"]) == (True, "Запинили mcp<2.")


def test_sources_say_whose_history_the_answer_came_from(hub):
    result = hub_ask.answer(hub, "как чинили CI?", composer=None)
    assert result["sources"][0]["owner"] == "egor"
    assert result["sources"][0]["project"] == "pr-review"


def test_sources_never_leak_server_paths(hub):
    result = hub_ask.answer(hub, "как чинили CI?", composer=None)
    assert "file_path" not in result["sources"][0]
    assert "/transcripts/" not in json.dumps(result["sources"])


def test_the_question_reaches_the_composer_with_the_fragments(hub):
    seen = {}

    def composer(req, chunks):
        seen.update(req=req, chunks=chunks)
        return "ok"

    hub_ask.answer(hub, "как чинили CI?", composer=composer)
    assert seen["req"]["question"] == "как чинили CI?"
    assert seen["chunks"][0]["snippet"].startswith("запинили")


def test_privacy_rule_states_what_must_not_be_repeated():
    rule = hub_ask.PRIVACY_RULE
    assert "health, family, money, private messages" in rule
    # attribution stays allowed — knowing whose work it was is the point
    assert "Saying whose work a finding came from is fine" in rule


def test_privacy_rule_is_attached_to_the_hub_composer(tmp_path, monkeypatch):
    captured = {}

    def fake_make_composer(system_extra=""):
        captured["extra"] = system_extra
        return lambda req, chunks: "ok"

    monkeypatch.setattr("session_recall.share.compose.make_composer",
                        fake_make_composer)
    hub = Hub(tmp_path / "h", recall_factory=lambda: FakeRecall())
    assert hub.composer is not None
    assert "health, family, money" in captured["extra"]


def test_ask_over_http_requires_a_question(url, hub):
    key = hub.keys.issue("egor")
    request = urllib.request.Request(
        url + "/v1/ask", data=json.dumps({}).encode(),
        headers={"Authorization": f"Bearer {key}"})
    with pytest.raises(urllib.error.HTTPError) as raised:
        urllib.request.urlopen(request, timeout=10)
    assert raised.value.code == 400


# -- MCP proxy ---------------------------------------------------------------

def test_remote_search_returns_real_anchors(remote):
    hits = remote.recall_search("как чинили", k=3)
    assert isinstance(hits, SearchResult)
    assert isinstance(hits[0], Anchor)
    assert hits[0].snippet.startswith("запинили")
    assert hits.degraded is None


def test_remote_expand_and_step_return_turns(remote):
    turns = remote.expand_around("s1", "u1")
    assert isinstance(turns[0], Turn) and turns[0].content == "как чинили?"
    assert remote.step("s1", "u1", "next")[0].content == "вот так"


def test_remote_grep_and_recent(remote):
    assert remote.grep("mcp<2")[0].snippet == "mcp<2"
    assert remote.recent_sessions()[0]["project"] == "pr-review"


def test_remote_ask(remote):
    assert "запинили" in remote.ask("как чинили CI?")["answer"]


def test_dates_are_resolved_client_side_and_sent_as_epochs(remote, hub):
    """The server has no business guessing the user's timezone."""
    remote.recall_search("q", start_ts=1785000000, end_ts=1785600000)
    _, kw = hub.recall.searched[-1]
    assert (kw["start_ts"], kw["end_ts"]) == (1785000000, 1785600000)


def test_a_dead_hub_says_so_in_one_sentence():
    from session_recall.hub.client import HubError
    dead = RemoteRecall(HubConfig("http://127.0.0.1:1", "sr_x_" + "0" * 32),
                        timeout=2)
    with pytest.raises(HubError, match="unreachable"):
        dead.recall_search("anything")


def test_mcp_picks_the_hub_only_when_joined(monkeypatch, tmp_path):
    """A solo install must not change behaviour, and must not even import
    the hub client's network path."""
    from session_recall import server as mcp_server

    monkeypatch.setattr(HubConfig, "load", staticmethod(lambda path=None: None))
    monkeypatch.setattr(mcp_server, "Recall", lambda *a, **k: "LOCAL")
    monkeypatch.setattr(mcp_server, "Store", lambda *a, **k: None)
    monkeypatch.setattr(mcp_server, "make_embedder", lambda: None)
    monkeypatch.setattr(mcp_server, "make_reranker", lambda: None)
    assert mcp_server.build_recall() == "LOCAL"

    monkeypatch.setattr(HubConfig, "load",
                        staticmethod(lambda path=None: HubConfig("http://h", "k")))
    assert isinstance(mcp_server.build_recall(), RemoteRecall)
