from dataclasses import dataclass

import pytest

from session_recall.share import identity as identity_mod
from session_recall.share import scanner
from session_recall.share.envelope import ShareState, make_request
from session_recall.share.transport import InMemoryTransport
from session_recall.share.trust import Peer, TrustStore
from session_recall.share.worker import (
    build_candidate, list_pending, load_candidate, poll_once, set_status)


# -- scanner -----------------------------------------------------------------
@pytest.mark.parametrize("kind,text", [
    ("aws-access-key", "creds: AKIAIOSFODNN7EXAMPLE done"),
    ("openai-key", "export OPENAI_API_KEY=sk-proj-abc123def456ghi789jkl"),
    ("github-token", "ghp_" + "a1B2" * 9 + " pushed"),
    ("jwt", "auth eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"),
    ("private-key-pem", "-----BEGIN OPENSSH PRIVATE KEY-----"),
    ("password-assignment", "POSTGRES_PASSWORD=hunter2hunter2"),
    ("connection-string", "postgres://app:s3cr3tpass@db.internal:5432/prod"),
])
def test_scanner_catches(kind, text):
    kinds = {f.kind for f in scanner.scan(text)}
    assert kind in kinds


def test_scanner_masks_excerpts():
    findings = scanner.scan("key AKIAIOSFODNN7EXAMPLE")
    assert findings and "EXAMPLE" not in findings[0].excerpt


def test_scanner_quiet_on_prose():
    text = ("we fixed the CI by pinning mcp below 2.0 because upstream removed "
            "the fastmcp module; see the decision doc for details")
    assert scanner.scan(text) == []


# -- worker ------------------------------------------------------------------
@dataclass
class FakeAnchor:
    session_id: str
    uuid: str
    role: str
    snippet: str
    score: float
    project: str
    when: int
    source: str = "claude"


def _anchor(project, snippet="how we fixed it", score=0.9):
    return FakeAnchor("sess-1234567890", "uuid-1", "assistant", snippet,
                      score, project, 1785000000)


@pytest.fixture
def world(tmp_path):
    maxim = identity_mod.create(tmp_path / "maxim", "maxim")
    egor = identity_mod.create(tmp_path / "egor", "egor")
    trust = TrustStore(tmp_path / "maxim" / "trust.json")
    b = egor.public_bundle()
    trust.add(Peer(name=b["name"], address=b["address"],
                   sign_pk=b["sign_pk"], box_pk=b["box_pk"]))
    trust.allow_project("session-recall")
    state = ShareState(tmp_path / "maxim" / "state.json")
    transport = InMemoryTransport()
    return maxim, egor, trust, state, transport, tmp_path / "maxim"


def _ask(egor, maxim, transport, question="how did you fix CI?", task=""):
    transport.post_mail(maxim.address,
                        make_request(egor, maxim.public_bundle(), question, task))


def test_poll_builds_candidate_with_provenance(world):
    maxim, egor, trust, state, transport, sdir = world
    _ask(egor, maxim, transport, task="debugging our pipeline")
    searcher = lambda q, k: [_anchor("session-recall"), _anchor("other-project")]
    cands = poll_once(maxim, trust, state, transport, searcher, sdir)
    assert len(cands) == 1
    c = cands[0]
    assert c.peer_name == "egor"
    assert c.question == "how did you fix CI?"
    assert [ch["project"] for ch in c.chunks] == ["session-recall"]  # scope filter
    assert "session-recall" in c.text and c.version
    assert c.status == "pending"
    assert load_candidate(sdir, c.id).question == c.question


def test_default_deny_empty_scope(world):
    maxim, egor, trust, state, transport, sdir = world
    trust.disallow_project("session-recall")
    _ask(egor, maxim, transport)
    called = []
    searcher = lambda q, k: called.append(q) or [_anchor("session-recall")]
    cands = poll_once(maxim, trust, state, transport, searcher, sdir)
    assert cands[0].chunks == []
    assert "share allow" in cands[0].text
    assert called == []  # empty allow-list: the index is not even queried


def test_secret_in_snippet_flagged(world):
    maxim, egor, trust, state, transport, sdir = world
    _ask(egor, maxim, transport)
    leaky = _anchor("session-recall",
                    snippet="deploy used AKIAIOSFODNN7EXAMPLE as the key")
    cands = poll_once(maxim, trust, state, transport, lambda q, k: [leaky], sdir)
    assert any(f["kind"] == "aws-access-key" for f in cands[0].findings)


def test_invalid_envelopes_produce_nothing(world, tmp_path):
    maxim, egor, trust, state, transport, sdir = world
    mallory = identity_mod.create(tmp_path / "m", "mallory")
    transport.post_mail(maxim.address,
                        make_request(mallory, maxim.public_bundle(), "gimme"))
    transport.post_mail(maxim.address, b"garbage-bytes")
    cands = poll_once(maxim, trust, state, transport,
                      lambda q, k: [_anchor("session-recall")], sdir)
    assert cands == [] and list_pending(sdir) == []


def test_rate_limited_requests_dropped(world):
    maxim, egor, trust, state, transport, sdir = world
    for i in range(5):
        _ask(egor, maxim, transport, question=f"q{i}")
    cands = poll_once(maxim, trust, state, transport,
                      lambda q, k: [_anchor("session-recall")], sdir)
    assert len(cands) == 3  # RATE_PER_MIN; the pump stops at the gate


def test_version_binds_to_content(world):
    maxim, egor, trust, state, transport, sdir = world
    _ask(egor, maxim, transport)
    cands = poll_once(maxim, trust, state, transport,
                      lambda q, k: [_anchor("session-recall")], sdir)
    c = cands[0]
    v1 = c.version
    c.text += " (edited)"
    assert c.compute_version() != v1  # /ok <old-version> must die after any edit


def test_status_lifecycle(world):
    maxim, egor, trust, state, transport, sdir = world
    _ask(egor, maxim, transport)
    c = poll_once(maxim, trust, state, transport,
                  lambda q, k: [_anchor("session-recall")], sdir)[0]
    assert [p.id for p in list_pending(sdir)] == [c.id]
    set_status(sdir, c.id, "approved")
    assert list_pending(sdir) == []
    assert load_candidate(sdir, c.id).status == "approved"


def test_question_length_capped(world):
    maxim, egor, trust, state, transport, sdir = world
    _ask(egor, maxim, transport, question="x" * 10000)
    cands = poll_once(maxim, trust, state, transport, lambda q, k: [], sdir)
    assert len(cands[0].question) == 2000
