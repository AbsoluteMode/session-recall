from dataclasses import dataclass

import pytest

from session_recall.share import approval
from session_recall.share import identity as identity_mod
from session_recall.share.envelope import ShareState, make_request, open_incoming
from session_recall.share.notify import NotifyLoop
from session_recall.share.telegram import TgConfig
from session_recall.share.transport import InMemoryTransport
from session_recall.share.trust import Peer, TrustStore
from session_recall.share.worker import load_candidate, poll_once

OWNER_CHAT = 111


@dataclass
class FakeAnchor:
    session_id: str = "sess-1234567890"
    uuid: str = "u1"
    role: str = "assistant"
    snippet: str = "we pinned mcp below 2 and CI went green"
    score: float = 0.9
    project: str = "session-recall"
    when: int = 1785000000
    source: str = "claude"


class FakeApi:
    def __init__(self):
        self.outbox: list[tuple[int, str]] = []
        self.updates: list[dict] = []
        self._mid = 0

    def send_message(self, chat_id, text, reply_to=None):
        self._mid += 1
        self.outbox.append((chat_id, text))
        return self._mid

    def get_updates(self, offset, timeout=25):
        pending = [u for u in self.updates if u["update_id"] >= offset]
        return pending

    # test helpers
    def owner_says(self, text, quoted_text, update_id, chat=OWNER_CHAT):
        self.updates.append({
            "update_id": update_id,
            "message": {"message_id": 1000 + update_id, "chat": {"id": chat},
                        "text": text,
                        "reply_to_message": {"text": quoted_text}}})


@pytest.fixture
def world(tmp_path):
    maxim = identity_mod.create(tmp_path / "maxim", "maxim")
    egor = identity_mod.create(tmp_path / "egor", "egor")
    trust = TrustStore(tmp_path / "maxim" / "trust.json")
    b = egor.public_bundle()
    trust.add(Peer(name=b["name"], address=b["address"],
                   sign_pk=b["sign_pk"], box_pk=b["box_pk"]))
    trust.allow_project("session-recall")
    egor_trust = TrustStore(tmp_path / "egor" / "trust.json")
    mb = maxim.public_bundle()
    egor_trust.add(Peer(name=mb["name"], address=mb["address"],
                        sign_pk=mb["sign_pk"], box_pk=mb["box_pk"]))
    return {
        "maxim": maxim, "egor": egor, "trust": trust, "egor_trust": egor_trust,
        "state": ShareState(tmp_path / "maxim" / "state.json"),
        "transport": InMemoryTransport(), "sdir": tmp_path / "maxim",
        "edir": tmp_path / "egor",
    }


def _incoming_candidate(w, question="how did you fix CI?"):
    w["transport"].post_mail(
        w["maxim"].address,
        make_request(w["egor"], w["maxim"].public_bundle(), question))
    return poll_once(w["maxim"], w["trust"], w["state"], w["transport"],
                     lambda q, k: [FakeAnchor()], w["sdir"])[0]


# -- approval core -----------------------------------------------------------
def test_approve_needs_exact_version(world):
    c = _incoming_candidate(world)
    assert approval.approve(world["sdir"], c.id, "00000000") is None
    assert load_candidate(world["sdir"], c.id).status == "pending"
    assert approval.approve(world["sdir"], c.id, c.version).status == "approved"


def test_approve_only_from_pending(world):
    c = _incoming_candidate(world)
    approval.approve(world["sdir"], c.id, c.version)
    assert approval.approve(world["sdir"], c.id, c.version) is None  # already approved


def test_preview_redacts_flagged_answers(world):
    c = _incoming_candidate(world)
    c.findings = [{"kind": "aws-access-key", "excerpt": "AKIAIOSF…"}]
    text = approval.preview(c)
    assert "withheld" in text and c.text not in text
    assert "SECRET FLAGS" in text
    clean = approval.preview(_incoming_candidate(world, question="q2"))
    assert "withheld" not in clean


def test_expire_stale(world):
    c = _incoming_candidate(world)
    later = c.created_at + approval.PENDING_TTL_S + 1
    expired = approval.expire_stale(world["sdir"], now=later)
    assert [e.id for e in expired] == [c.id]
    assert approval.approve(world["sdir"], c.id, c.version) is None


def test_dispatch_sends_only_decided(world):
    c1 = _incoming_candidate(world, "q1")
    _c2 = _incoming_candidate(world, "q2")            # stays pending
    approval.approve(world["sdir"], c1.id, c1.version)
    sent = approval.dispatch(world["maxim"], world["trust"], world["sdir"],
                             world["transport"])
    assert [s.id for s in sent] == [c1.id]
    assert load_candidate(world["sdir"], c1.id).status == "sent"

    # egor receives a verifiable response bound to his original request
    inbox = world["transport"].fetch_mail(world["egor"].address)
    assert len(inbox) == 1
    got = open_incoming(world["egor"], world["egor_trust"],
                        ShareState(world["edir"] / "state.json"), inbox[0])
    assert got.kind == "resp"
    assert got.in_reply_to == c1.reply_nonce
    assert "mcp below 2" in got.body["text"]


def test_dispatch_drops_revoked_peer(world):
    c = _incoming_candidate(world)
    approval.approve(world["sdir"], c.id, c.version)
    world["trust"].revoke("egor")
    sent = approval.dispatch(world["maxim"], world["trust"], world["sdir"],
                             world["transport"])
    assert sent == []
    assert load_candidate(world["sdir"], c.id).status == "dropped-revoked"
    assert world["transport"].fetch_mail(world["egor"].address) == []


def test_declined_send_is_labelled(world):
    c = _incoming_candidate(world)
    approval.reject(world["sdir"], c.id, "not sharing that")
    approval.dispatch(world["maxim"], world["trust"], world["sdir"],
                      world["transport"])
    assert load_candidate(world["sdir"], c.id).status == "declined-sent"
    inbox = world["transport"].fetch_mail(world["egor"].address)
    got = open_incoming(world["egor"], world["egor_trust"],
                        ShareState(world["edir"] / "state.json"), inbox[0])
    assert got.body["text"].startswith("(declined)")


# -- notify loop -------------------------------------------------------------
def _loop(w, api):
    return NotifyLoop(api, w["maxim"], w["trust"], w["state"], w["transport"],
                      lambda q, k: [FakeAnchor()], w["sdir"],
                      TgConfig(token="t", chat_id=OWNER_CHAT))


def test_loop_preview_ok_send(world):
    api = FakeApi()
    loop = _loop(world, api)
    world["transport"].post_mail(
        world["maxim"].address,
        make_request(world["egor"], world["maxim"].public_bundle(), "how?"))
    loop.tick()
    assert len(api.outbox) == 1
    preview_text = api.outbox[0][1]
    cand_id, version = preview_text[1:9], preview_text[11:19]

    api.owner_says(f"/ok {version}", preview_text, update_id=1)
    loop.tick()
    texts = [t for _, t in api.outbox]
    assert any("approved" in t for t in texts)
    assert any(t.startswith("sent to egor") for t in texts)
    inbox = world["transport"].fetch_mail(world["egor"].address)
    assert len(inbox) == 1


def test_loop_rejects_wrong_version_and_bare_ok(world):
    api = FakeApi()
    loop = _loop(world, api)
    world["transport"].post_mail(
        world["maxim"].address,
        make_request(world["egor"], world["maxim"].public_bundle(), "how?"))
    loop.tick()
    preview_text = api.outbox[0][1]

    api.owner_says("/ok 00000000", preview_text, update_id=1)
    api.owner_says("/ok", preview_text, update_id=2)
    loop.tick()
    texts = [t for _, t in api.outbox]
    assert any("not approved" in t for t in texts)
    assert any("usage:" in t for t in texts)
    assert world["transport"].fetch_mail(world["egor"].address) == []


def test_loop_ignores_strangers(world):
    api = FakeApi()
    loop = _loop(world, api)
    world["transport"].post_mail(
        world["maxim"].address,
        make_request(world["egor"], world["maxim"].public_bundle(), "how?"))
    loop.tick()
    preview_text = api.outbox[0][1]
    _, version = preview_text[1:9], preview_text[11:19]

    api.owner_says(f"/ok {version}", preview_text, update_id=1, chat=999)
    loop.tick()
    assert world["transport"].fetch_mail(world["egor"].address) == []
    assert not any("approved" in t for _, t in api.outbox)


def test_loop_no_declines_with_reason(world):
    api = FakeApi()
    loop = _loop(world, api)
    world["transport"].post_mail(
        world["maxim"].address,
        make_request(world["egor"], world["maxim"].public_bundle(), "how?"))
    loop.tick()
    preview_text = api.outbox[0][1]

    api.owner_says("/no ask me tomorrow", preview_text, update_id=1)
    loop.tick()
    inbox = world["transport"].fetch_mail(world["egor"].address)
    got = open_incoming(world["egor"], world["egor_trust"],
                        ShareState(world["edir"] / "state.json"), inbox[0])
    assert got.body["text"] == "(declined) ask me tomorrow"


def test_loop_offset_advances_no_replay_of_commands(world):
    api = FakeApi()
    loop = _loop(world, api)
    world["transport"].post_mail(
        world["maxim"].address,
        make_request(world["egor"], world["maxim"].public_bundle(), "how?"))
    loop.tick()
    preview_text = api.outbox[0][1]
    _, version = preview_text[1:9], preview_text[11:19]
    api.owner_says(f"/ok {version}", preview_text, update_id=7)
    loop.tick()
    handled_twice = loop.tick()   # same update must not be seen again
    assert handled_twice["handled"] == 0
