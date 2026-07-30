import json

import pytest

from session_recall.share import envelope
from session_recall.share import identity as identity_mod
from session_recall.share.crypto import b64, unb64, canonical
from session_recall.share.envelope import ShareState, open_incoming, make_request, make_response
from session_recall.share.trust import Peer, TrustStore


@pytest.fixture
def world(tmp_path):
    """maxim and egor, mutually trusted."""
    maxim = identity_mod.create(tmp_path / "a", "maxim")
    egor = identity_mod.create(tmp_path / "b", "egor")
    maxim_trust = TrustStore(tmp_path / "a" / "trust.json")
    egor_trust = TrustStore(tmp_path / "b" / "trust.json")
    maxim_trust.add(_peer(egor))
    egor_trust.add(_peer(maxim))
    return maxim, egor, maxim_trust, egor_trust, ShareState(tmp_path / "a" / "state.json")


def _peer(ident) -> Peer:
    b = ident.public_bundle()
    return Peer(name=b["name"], address=b["address"],
                sign_pk=b["sign_pk"], box_pk=b["box_pk"])


def test_request_roundtrip(world):
    maxim, egor, maxim_trust, _, state = world
    raw = make_request(egor, _peer(maxim), "how did you fix the CI?", task="debug")
    got = open_incoming(maxim, maxim_trust, state, raw)
    assert got is not None
    assert got.kind == "req"
    assert got.body == {"question": "how did you fix the CI?", "task": "debug"}
    assert got.peer.name == "egor"


def test_response_roundtrip(world):
    maxim, egor, maxim_trust, egor_trust, state = world
    req = make_request(egor, _peer(maxim), "q")
    got = open_incoming(maxim, maxim_trust, state, req)
    resp = make_response(maxim, _peer(egor), "the answer", in_reply_to=got.nonce)
    egor_state = ShareState(maxim_trust.path.parent / "egor-state.json")
    got_resp = open_incoming(egor, egor_trust, egor_state, resp)
    assert got_resp.kind == "resp"
    assert got_resp.body == {"text": "the answer"}
    assert got_resp.in_reply_to == got.nonce


def test_unknown_sender_dropped(world, tmp_path):
    maxim, _, maxim_trust, _, state = world
    mallory = identity_mod.create(tmp_path / "m", "mallory")
    raw = make_request(mallory, _peer(maxim), "hi")
    assert open_incoming(maxim, maxim_trust, state, raw) is None


def test_revoked_sender_dropped(world):
    maxim, egor, maxim_trust, _, state = world
    maxim_trust.revoke("egor")
    raw = make_request(egor, _peer(maxim), "q")
    assert open_incoming(maxim, maxim_trust, state, raw) is None


def test_wrong_recipient_dropped(world, tmp_path):
    """Envelope addressed to someone else must not open even with valid crypto."""
    maxim, egor, maxim_trust, _, state = world
    other = identity_mod.create(tmp_path / "o", "other")
    raw = make_request(egor, _peer(other), "q")
    assert open_incoming(maxim, maxim_trust, state, raw) is None


def test_tampered_payload_dropped(world):
    maxim, egor, maxim_trust, _, state = world
    env = json.loads(make_request(egor, _peer(maxim), "q"))
    env["ts"] = env["ts"] + 1  # any signed-field change must kill the signature
    assert open_incoming(maxim, maxim_trust, state,
                         json.dumps(env).encode()) is None


def test_signature_from_wrong_key_dropped(world, tmp_path):
    maxim, egor, maxim_trust, _, state = world
    mallory = identity_mod.create(tmp_path / "m", "mallory")
    env = json.loads(make_request(egor, _peer(maxim), "q"))
    env.pop("sig")
    env["from"] = egor.address  # claims to be egor…
    sig = mallory.signing_key.sign(canonical(env)).signature  # …signed by mallory
    env["sig"] = b64(sig)
    assert open_incoming(maxim, maxim_trust, state,
                         json.dumps(env).encode()) is None


def test_stale_timestamp_dropped(world):
    maxim, egor, maxim_trust, _, state = world
    raw = make_request(egor, _peer(maxim), "q")
    now = json.loads(raw)["ts"] + envelope.REQ_TTL_S + 1
    assert open_incoming(maxim, maxim_trust, state, raw, now=now) is None


def test_replay_dropped(world):
    maxim, egor, maxim_trust, _, state = world
    raw = make_request(egor, _peer(maxim), "q")
    assert open_incoming(maxim, maxim_trust, state, raw) is not None
    assert open_incoming(maxim, maxim_trust, state, raw) is None


@pytest.fixture
def clock(monkeypatch):
    """Simulated wall clock driving BOTH envelope creation (ts) and the inbound
    gate (now) — otherwise every simulated-time test trips the staleness check."""
    state = {"t": 1_800_000_000.0}
    monkeypatch.setattr(envelope.time, "time", lambda: state["t"])

    def advance(dt: float) -> float:
        state["t"] += dt
        return state["t"]
    return advance


def test_rate_limit_per_minute(world, clock):
    maxim, egor, maxim_trust, _, state = world
    for i in range(envelope.RATE_PER_MIN):
        raw = make_request(egor, _peer(maxim), f"q{i}")
        assert open_incoming(maxim, maxim_trust, state, raw, now=clock(1)) is not None
    raw = make_request(egor, _peer(maxim), "one too many")
    assert open_incoming(maxim, maxim_trust, state, raw, now=clock(1)) is None
    # …but the window slides: a minute later requests flow again
    raw = make_request(egor, _peer(maxim), "later")
    assert open_incoming(maxim, maxim_trust, state, raw, now=clock(60)) is not None


def test_rate_limit_per_day(world, clock):
    maxim, egor, maxim_trust, _, state = world
    for i in range(envelope.RATE_PER_DAY):
        # spread far apart so the minute window never trips
        clock(120)
        raw = make_request(egor, _peer(maxim), f"q{i}")
        assert open_incoming(maxim, maxim_trust, state, raw, now=clock(0)) is not None
    raw = make_request(egor, _peer(maxim), "pump")
    assert open_incoming(maxim, maxim_trust, state, raw, now=clock(120)) is None


def test_responses_not_rate_limited(world, clock):
    """Rate caps guard the asking side; replies to our own questions must not
    starve just because the peer answered a burst of them."""
    maxim, egor, maxim_trust, egor_trust, _ = world
    egor_state = ShareState(egor_trust.path.parent / "state.json")
    for i in range(envelope.RATE_PER_MIN + 2):
        resp = make_response(maxim, _peer(egor), f"a{i}", in_reply_to=f"n{i}")
        assert open_incoming(egor, egor_trust, egor_state, resp, now=clock(1)) is not None


def test_garbage_bytes_dropped(world):
    maxim, _, maxim_trust, _, state = world
    for garbage in (b"", b"\xff\xfe", b"[]", b"{}", b'{"v":2}'):
        assert open_incoming(maxim, maxim_trust, state, garbage) is None


def test_state_survives_reload(world, tmp_path):
    """Replay protection must hold across process restarts."""
    maxim, egor, maxim_trust, _, state = world
    raw = make_request(egor, _peer(maxim), "q")
    assert open_incoming(maxim, maxim_trust, state, raw) is not None
    reloaded = ShareState(state.path)
    assert open_incoming(maxim, maxim_trust, reloaded, raw) is None
