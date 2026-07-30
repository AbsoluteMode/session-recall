"""The relay is exercised over real HTTP: ThreadingHTTPServer on an ephemeral
port, the actual HttpRelayTransport as the client."""

import threading
import time
import urllib.error
from http.server import ThreadingHTTPServer

import pytest

from session_recall.share import identity as identity_mod
from session_recall.share import pairing, relay
from session_recall.share.envelope import ShareState, make_request, open_incoming
from session_recall.share.transport import HttpRelayTransport
from session_recall.share.trust import Peer, TrustStore


@pytest.fixture
def server(tmp_path):
    clockbox = {"t": time.time()}
    store = relay.RelayStore(tmp_path / "relay-data", clock=lambda: clockbox["t"])
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), relay.make_handler(store))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", clockbox
    httpd.shutdown()


@pytest.fixture
def users(tmp_path):
    maxim = identity_mod.create(tmp_path / "maxim", "maxim")
    egor = identity_mod.create(tmp_path / "egor", "egor")
    return maxim, egor


def test_slot_roundtrip_and_miss(server, users):
    url, _ = server
    t = HttpRelayTransport(url)
    t.put_slot("pair-a-abc", b"sealed-bundle")
    assert t.get_slot("pair-a-abc") == b"sealed-bundle"
    assert t.get_slot("pair-a-nope") is None


def test_invalid_slot_name_rejected(server):
    url, _ = server
    t = HttpRelayTransport(url)
    assert t.get_slot("..%2f..%2fetc") is None


def test_mail_requires_signature(server, users):
    url, _ = server
    maxim, egor = users
    anon = HttpRelayTransport(url)                       # no identity
    anon.post_mail(maxim.address, b"envelope-1")
    assert anon.fetch_mail(maxim.address) == []          # unsigned fetch: silence
    owner = HttpRelayTransport(url, identity=maxim)
    assert owner.fetch_mail(maxim.address) == [b"envelope-1"]
    assert owner.fetch_mail(maxim.address) == []         # consumed


def test_wrong_key_cannot_steal_mail(server, users, tmp_path):
    url, _ = server
    maxim, egor = users
    owner = HttpRelayTransport(url, identity=maxim)
    assert owner.fetch_mail(maxim.address) == []         # first fetch binds the key
    HttpRelayTransport(url).post_mail(maxim.address, b"secret-envelope")
    mallory = identity_mod.create(tmp_path / "m", "mallory")
    thief = HttpRelayTransport(url, identity=mallory)
    assert thief.fetch_mail(maxim.address) == []         # wrong key: silence
    assert owner.fetch_mail(maxim.address) == [b"secret-envelope"]  # still there


def test_stale_fetch_signature_rejected(server, users, monkeypatch):
    url, _ = server
    maxim, _ = users
    HttpRelayTransport(url).post_mail(maxim.address, b"envelope")
    real = time.time
    monkeypatch.setattr(relay.time, "time",
                        lambda: real() - relay.FETCH_SIG_WINDOW_S - 5)
    stale = HttpRelayTransport(url, identity=maxim)      # signs with skewed clock
    assert stale.fetch_mail(maxim.address) == []
    monkeypatch.undo()
    assert HttpRelayTransport(url, identity=maxim).fetch_mail(
        maxim.address) == [b"envelope"]


def test_oversized_blob_rejected(server):
    url, _ = server
    t = HttpRelayTransport(url)
    with pytest.raises(urllib.error.HTTPError):
        t.put_slot("pair-a-big", b"x" * (relay.MAX_BLOB + 1))


def test_mailbox_cap(server, users, monkeypatch):
    url, _ = server
    maxim, _ = users
    monkeypatch.setattr(relay, "MAX_MAILBOX", 3)
    anon = HttpRelayTransport(url)
    for i in range(5):
        anon.post_mail(maxim.address, f"e{i}".encode())
    got = HttpRelayTransport(url, identity=maxim).fetch_mail(maxim.address)
    # frozen test clock ⇒ identical name prefixes ⇒ order is not guaranteed;
    # the cap keeps the first three accepted, overflow dropped silently
    assert sorted(got) == [b"e0", b"e1", b"e2"]


def test_slot_ttl(server):
    url, clockbox = server
    t = HttpRelayTransport(url)
    t.put_slot("pair-a-old", b"sealed")
    clockbox["t"] += relay.SLOT_TTL_S + 5
    assert t.get_slot("pair-a-old") is None


def test_full_ceremony_and_request_over_http(server, users, tmp_path):
    """The whole v1 loop over a live relay: pair, trust, ask, gate."""
    url, _ = server
    maxim, egor = users
    tm = HttpRelayTransport(url, identity=maxim)
    te = HttpRelayTransport(url, identity=egor)
    mdir, edir = tmp_path / "maxim", tmp_path / "egor"

    code = pairing.start_invite(maxim, tm, mdir)
    joined = pairing.join(egor, te, edir, code)
    completed = pairing.complete_invite(maxim, tm, mdir)
    assert joined.sas == completed.sas

    maxim_trust = TrustStore(mdir / "trust.json")
    b = joined.bundle  # egor trusts maxim's bundle; maxim trusts egor's
    maxim_trust.add(Peer(name=completed.bundle["name"],
                         address=completed.bundle["address"],
                         sign_pk=completed.bundle["sign_pk"],
                         box_pk=completed.bundle["box_pk"]))

    te.post_mail(b["address"], make_request(egor, b, "how did you fix CI?"))
    inbox = tm.fetch_mail(maxim.address)
    assert len(inbox) == 1
    got = open_incoming(maxim, maxim_trust, ShareState(mdir / "state.json"), inbox[0])
    assert got is not None and got.body["question"] == "how did you fix CI?"
