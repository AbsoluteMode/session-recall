"""Conversations: follow-ups without re-asking, and the owner answering in
their own words — which is the whole point, because the real answer is often
not in the index at all."""

from dataclasses import dataclass

import pytest

from session_recall.share import approval, compose, thread as thread_mod
from session_recall.share import identity as identity_mod
from session_recall.share.envelope import ShareState, make_request, open_incoming
from session_recall.share.transport import InMemoryTransport
from session_recall.share.trust import Peer, TrustStore
from session_recall.share.worker import load_candidate, poll_once


@dataclass
class FakeAnchor:
    session_id: str = "sess-1234567890"
    uuid: str = "u1"
    role: str = "assistant"
    snippet: str = "запинили mcp<2 в PR #13"
    score: float = 0.9
    project: str = "session-recall"
    when: int = 1785000000
    source: str = "claude"


@pytest.fixture
def world(tmp_path):
    maxim = identity_mod.create(tmp_path / "maxim", "maxim")
    egor = identity_mod.create(tmp_path / "egor", "egor")
    mt = TrustStore(tmp_path / "maxim" / "trust.json")
    b = egor.public_bundle()
    mt.add(Peer(name=b["name"], address=b["address"],
                sign_pk=b["sign_pk"], box_pk=b["box_pk"]))
    mt.allow_project("session-recall")
    et = TrustStore(tmp_path / "egor" / "trust.json")
    mb = maxim.public_bundle()
    et.add(Peer(name=mb["name"], address=mb["address"],
                sign_pk=mb["sign_pk"], box_pk=mb["box_pk"]))
    return {"maxim": maxim, "egor": egor, "mt": mt, "et": et,
            "state": ShareState(tmp_path / "maxim" / "state.json"),
            "transport": InMemoryTransport(),
            "mdir": tmp_path / "maxim", "edir": tmp_path / "egor"}


def _ask(w, question="как чинили CI?", thread=""):
    w["transport"].post_mail(w["maxim"].address, make_request(
        w["egor"], w["maxim"].public_bundle(), question,
        task="поднимаю relay", problem="ModuleNotFoundError", thread=thread))
    return poll_once(w["maxim"], w["mt"], w["state"], w["transport"],
                     lambda q, k: [FakeAnchor()], w["mdir"])


def test_first_ask_opens_a_thread(world):
    cand = _ask(world)[0]
    assert cand.thread
    convo = thread_mod.load(world["mdir"], cand.thread)
    assert convo.peer_name == "egor"
    assert convo.turns[0]["author"] == "peer"


def test_follow_up_joins_the_same_thread(world):
    first = _ask(world)[0]
    second = _ask(world, "а пин или миграция?", thread=first.thread)[0]
    assert second.thread == first.thread
    convo = thread_mod.load(world["mdir"], first.thread)
    assert len(convo.turns) == 2


def test_history_reaches_the_composer(world):
    first = _ask(world)[0]
    seen = {}
    world["transport"].post_mail(world["maxim"].address, make_request(
        world["egor"], world["maxim"].public_bundle(), "а точнее?",
        thread=first.thread))

    def composer(req, chunks, turns=None):
        seen["turns"] = turns
        return "ответ с учётом предыдущего"

    poll_once(world["maxim"], world["mt"], world["state"], world["transport"],
              lambda q, k: [FakeAnchor()], world["mdir"], composer=composer)
    assert seen["turns"], "the composer must see what was already asked"
    assert "как чинили CI?" in seen["turns"][0]["text"]


def test_owner_can_answer_in_their_own_words(world):
    """No approval step — typing it IS the authorization."""
    cand = _ask(world)[0]
    ok = approval.own_message(world["maxim"], world["mt"], world["mdir"],
                              world["transport"], cand.thread,
                              "там дело было не в пине, а в версии ноды")
    assert ok
    inbox = world["transport"].fetch_mail(world["egor"].address)
    got = open_incoming(world["egor"], world["et"],
                        ShareState(world["edir"] / "state.json"), inbox[0])
    assert got.body["text"].startswith("там дело было")
    assert got.body["thread"] == cand.thread


def test_owner_message_fails_closed_on_revoked_peer(world):
    cand = _ask(world)[0]
    world["mt"].revoke("egor")
    assert approval.own_message(world["maxim"], world["mt"], world["mdir"],
                               world["transport"], cand.thread, "ответ") is False
    assert world["transport"].fetch_mail(world["egor"].address) == []


def test_closed_thread_refuses_new_turns(world):
    cand = _ask(world)[0]
    convo = thread_mod.load(world["mdir"], cand.thread)
    convo.closed = True
    thread_mod.save(world["mdir"], convo)
    assert _ask(world, "ещё вопрос", thread=cand.thread) == []
    assert approval.own_message(world["maxim"], world["mt"], world["mdir"],
                               world["transport"], cand.thread, "ответ") is False


def test_thread_closes_after_too_many_turns(world):
    convo = thread_mod.Thread(id="abc", peer_address="x", peer_name="egor",
                              created_at=0.0)
    for i in range(thread_mod.MAX_TURNS):
        convo.append("peer", f"q{i}")
    assert convo.should_close()


# -- what actually crosses the wire ------------------------------------------
def test_response_carries_no_transcript_or_session_ids(world):
    cand = _ask(world)[0]
    approval.approve(world["mdir"], cand.id, cand.version)
    approval.dispatch(world["maxim"], world["mt"], world["mdir"],
                      world["transport"])
    inbox = world["transport"].fetch_mail(world["egor"].address)
    got = open_incoming(world["egor"], world["et"],
                        ShareState(world["edir"] / "state.json"), inbox[0])
    assert got.body["sources"] == 1        # grounded-vs-guess signal survives
    assert "session_id" not in got.body and "refs" not in got.body
    assert "sess-1234567890" not in str(got.body)


def test_composer_is_told_not_to_cite_sessions():
    assert "Never cite session ids" in compose._SYSTEM
