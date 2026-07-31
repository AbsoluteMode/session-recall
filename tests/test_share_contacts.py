"""The contacts layer: petnames the owner chose, scope per contact, one inbox
per pairing, and the pause switch.

Why each exists — petname: the name in a bundle is the peer's own claim, so a
second contact could introduce themselves as the first; the owner's petname is
what every preview and prompt refers to. Per-contact scope: what one colleague
should know is not what another should. Per-pair inboxes: the relay sees
unrelated islands instead of a social graph, and revoking closes the channel
itself. Pause: a brake the owner can pull without unpairing anyone."""

from dataclasses import dataclass

import pytest

from session_recall.share import identity as identity_mod
from session_recall.share import pairing
from session_recall.share.envelope import ShareState, make_request, open_incoming
from session_recall.share.transport import InMemoryTransport
from session_recall.share.trust import Peer, TrustStore
from session_recall.share.worker import poll_once


@dataclass
class FakeAnchor:
    session_id: str = "sess-1234567890"
    uuid: str = "u1"
    role: str = "assistant"
    snippet: str = "how we fixed it"
    score: float = 0.9
    project: str = "alpha"
    when: int = 1785000000
    source: str = "claude"


def _enroll(trust: TrustStore, ident, petname: str, local_address: str = "") -> Peer:
    b = ident.public_bundle()
    peer = Peer(name=b["name"], address=b["address"], sign_pk=b["sign_pk"],
                box_pk=b["box_pk"], petname=petname, local_address=local_address)
    trust.add(peer)
    return peer


@pytest.fixture
def world(tmp_path):
    maxim = identity_mod.create(tmp_path / "maxim", "maxim")
    egor = identity_mod.create(tmp_path / "egor", "egor")
    lena = identity_mod.create(tmp_path / "lena", "lena")
    trust = TrustStore(tmp_path / "maxim" / "trust.json")
    return {"maxim": maxim, "egor": egor, "lena": lena, "trust": trust,
            "state": ShareState(tmp_path / "maxim" / "state.json"),
            "transport": InMemoryTransport(), "mdir": tmp_path / "maxim"}


# -- petnames -----------------------------------------------------------------
def test_petname_is_what_everything_refers_to(world):
    """The peer introduces themselves as 'maxim' — the OWNER's name for them is
    what the candidate (and so every preview) carries."""
    impostor = identity_mod.create(world["mdir"].parent / "i", "maxim")
    _enroll(world["trust"], impostor, "egor-work")
    world["trust"].allow_project("alpha")
    world["transport"].post_mail(world["maxim"].address, make_request(
        impostor, world["maxim"].public_bundle(), "q?"))
    cand = poll_once(world["maxim"], world["trust"], world["state"],
                     world["transport"], lambda q, k: [FakeAnchor()],
                     world["mdir"])[0]
    assert cand.peer_name == "egor-work"    # not the claimed "maxim"
    assert world["trust"].get("egor-work").name == "maxim"


def test_duplicate_petname_rejected(world):
    _enroll(world["trust"], world["egor"], "egor")
    with pytest.raises(ValueError, match="unique"):
        _enroll(world["trust"], world["lena"], "egor")


def test_get_prefers_petname_over_claimed_name(world):
    """lena's petname is egor's claimed name — lookups must never cross."""
    egor = _enroll(world["trust"], world["egor"], "lena")
    lena = _enroll(world["trust"], world["lena"], "kate")
    assert world["trust"].get("lena").address == egor.address
    assert world["trust"].get("kate").address == lena.address


# -- per-contact scope --------------------------------------------------------
def test_scope_is_per_contact(world):
    """egor may see alpha, lena may see beta; neither sees the other's."""
    egor = _enroll(world["trust"], world["egor"], "egor")
    lena = _enroll(world["trust"], world["lena"], "lena")
    world["trust"].allow_project("alpha", egor)
    world["trust"].allow_project("beta", lena)
    anchors = [FakeAnchor(project="alpha"), FakeAnchor(project="beta")]

    for ident, expect in ((world["egor"], "alpha"), (world["lena"], "beta")):
        world["transport"].post_mail(world["maxim"].address, make_request(
            ident, world["maxim"].public_bundle(), "q?"))
        cand = poll_once(world["maxim"], world["trust"], world["state"],
                         world["transport"], lambda q, k: anchors,
                         world["mdir"])[0]
        assert [c["project"] for c in cand.chunks] == [expect]


def test_all_bucket_reaches_every_contact(world):
    egor = _enroll(world["trust"], world["egor"], "egor")
    world["trust"].allow_project("common")          # peer=None → the all bucket
    assert world["trust"].projects_for(egor) == ["common"]


def test_no_grants_means_nothing(world):
    egor = _enroll(world["trust"], world["egor"], "egor")
    assert world["trust"].projects_for(egor) == []
    assert world["trust"].projects_for(None) == []


# -- pause --------------------------------------------------------------------
def test_pause_leaves_mail_at_the_relay(world):
    """Paused must mean not-even-fetched: consuming mail is destructive, so the
    question has to survive the pause and be answered after resume."""
    _enroll(world["trust"], world["egor"], "egor")
    world["trust"].allow_project("alpha")
    world["transport"].post_mail(world["maxim"].address, make_request(
        world["egor"], world["maxim"].public_bundle(), "q?"))

    world["trust"].set_paused(True)
    assert poll_once(world["maxim"], world["trust"], world["state"],
                     world["transport"], lambda q, k: [FakeAnchor()],
                     world["mdir"]) == []
    assert world["transport"].mail                  # still parked, not consumed

    world["trust"].set_paused(False)
    cands = poll_once(world["maxim"], world["trust"], world["state"],
                      world["transport"], lambda q, k: [FakeAnchor()],
                      world["mdir"])
    assert len(cands) == 1


def test_pause_survives_reload(world):
    world["trust"].set_paused(True)
    assert TrustStore(world["trust"].path).paused is True


# -- per-pair inboxes ---------------------------------------------------------
def _pair(tmp_path):
    """Full ceremony; both sides enroll with petnames. Returns everything the
    round-trip tests need."""
    a = identity_mod.create(tmp_path / "a", "maxim")
    b = identity_mod.create(tmp_path / "b", "egor")
    t = InMemoryTransport()
    code = pairing.start_invite(a, t, tmp_path / "a")
    pairing.join(b, t, tmp_path / "b", code)
    pairing.complete_invite(a, t, tmp_path / "a")

    def enroll(trust_path, own_dir, petname):
        cand = pairing.pending_peer(own_dir)
        trust = TrustStore(trust_path)
        bb = cand["bundle"]
        trust.add(Peer(name=bb["name"], address=bb["address"],
                       sign_pk=bb["sign_pk"], box_pk=bb["box_pk"],
                       petname=petname,
                       local_address=cand.get("local_address", "")))
        return trust

    at = enroll(tmp_path / "a" / "trust.json", tmp_path / "a", "egor")
    bt = enroll(tmp_path / "b" / "trust.json", tmp_path / "b", "maxim")
    return a, b, at, bt, t


def test_pairing_mints_fresh_addresses_per_pair(tmp_path):
    a, b, at, bt, _ = _pair(tmp_path)
    a_for_b = at.peers()[0].local_address    # where a listens for b
    b_for_a = bt.peers()[0].local_address    # where b listens for a
    minted = {a_for_b, b_for_a}
    assert all(minted)
    # neither side's identity address is on the wire for this pair
    assert not minted & {a.address, b.address}
    # and the addresses cross-reference: what a listens on is where b sends
    assert bt.peers()[0].address == a_for_b
    assert at.peers()[0].address == b_for_a


def test_roundtrip_over_pair_addresses(tmp_path):
    a, b, at, bt, t = _pair(tmp_path)
    peer_a = bt.peers()[0]                   # b's record of a
    t.post_mail(peer_a.address, make_request(b, peer_a, "q?"))
    # mail sits in the pair inbox, not the identity inbox
    assert peer_a.address in t.mail and a.address not in t.mail
    state = ShareState(tmp_path / "a" / "state.json")
    cands = poll_once(a, at, state, t, lambda q, k: [], tmp_path / "a")
    assert len(cands) == 1 and cands[0].peer_name == "egor"


def test_envelope_to_identity_inbox_rejected_when_pair_inbox_exists(tmp_path):
    """A valid-crypto envelope aimed at the identity address must die once the
    pair has its own inbox — cross-inbox delivery is a misroute by definition."""
    a, b, at, bt, t = _pair(tmp_path)
    peer_a = bt.peers()[0]
    forged = Peer(**{**peer_a.__dict__, "address": a.address})
    raw = make_request(b, forged, "q?")
    state = ShareState(tmp_path / "a" / "state.json")
    assert open_incoming(a, at, state, raw) is None


def test_revoke_stops_listening_on_their_inbox(tmp_path):
    a, _, at, _, _ = _pair(tmp_path)
    inbox = at.peers()[0].local_address
    assert inbox in at.inbox_addresses(a)
    at.revoke("egor")
    assert inbox not in at.inbox_addresses(a)


def test_legacy_peer_without_pair_inbox_still_works(world):
    """Pre-contacts stores have no local_address — the identity inbox stays a
    working fallback so an upgrade breaks no existing pairing."""
    _enroll(world["trust"], world["egor"], "egor")   # local_address=""
    assert world["trust"].inbox_addresses(world["maxim"]) == [world["maxim"].address]
    world["transport"].post_mail(world["maxim"].address, make_request(
        world["egor"], world["maxim"].public_bundle(), "q?"))
    cands = poll_once(world["maxim"], world["trust"], world["state"],
                      world["transport"], lambda q, k: [], world["mdir"])
    assert len(cands) == 1
