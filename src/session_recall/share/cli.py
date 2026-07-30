"""`session-recall share …` — the human-only surface of the share protocol.

Every trust mutation lives here and only here: no MCP tool reaches identity,
pairing or the trust store (gate §2). The ceremony is deliberately split into
invite/join/complete → read the SAS aloud → trust, so the human check cannot
be skipped by automation.
"""

import argparse
import os

from .. import config
from . import identity as identity_mod
from . import pairing
from .pairing import PairingError
from .transport import from_env
from .trust import Peer, TrustStore

_TRANSPORT_HINT = (
    "no transport configured: set SESSION_RECALL_RELAY_URL (relay) or "
    "SESSION_RECALL_SHARE_TRANSPORT_DIR (shared folder)")


def add_parser(sub) -> None:
    shp = sub.add_parser("share", help="p2p recall sharing: pairing and trust")
    ssub = shp.add_subparsers(dest="share_cmd", required=True)
    ip = ssub.add_parser("init", help="create this device's share identity")
    ip.add_argument("name", help="how you introduce yourself to peers")
    ssub.add_parser("invite", help="start pairing; prints a one-time code")
    jp = ssub.add_parser("join", help="accept an invite code from a peer")
    jp.add_argument("code")
    ssub.add_parser("complete", help="finish pairing after the peer joined")
    ssub.add_parser("trust", help="confirm the SAS matched; enroll the peer")
    ssub.add_parser("devices", help="list trusted peers")
    rp = ssub.add_parser("revoke", help="revoke a peer by name or address")
    rp.add_argument("peer")
    ap = ssub.add_parser("allow", help="mark a project shareable (no arg: list)")
    ap.add_argument("project", nargs="?")
    ap.add_argument("--remove", action="store_true")
    rlp = ssub.add_parser("relay", help="run the relay server (blind blob store)")
    rlp.add_argument("--port", type=int, default=8787)
    rlp.add_argument("--data", default=None,
                     help="storage dir (default: <data-dir>/share-relay)")


def _sas_block(res: pairing.PairingResult) -> str:
    return (f"paired with: {res.bundle['name']} ({res.bundle['address']})\n"
            f"\n    SAS code: {res.sas}\n\n"
            "Read it aloud to each other over a channel you trust. If BOTH see "
            "the same code, each side runs: session-recall share trust")


def run(args: argparse.Namespace) -> int:
    sdir = config.DATA_DIR / "share"
    cmd = args.share_cmd

    if cmd == "relay":
        from pathlib import Path
        from .relay import serve
        serve(args.port, Path(args.data) if args.data else config.DATA_DIR / "share-relay")
        return 0

    if cmd == "init":
        try:
            ident = identity_mod.create(sdir, args.name)
        except FileExistsError as exc:
            print(exc)
            return 1
        print(f"share identity created for {ident.name}\n"
              f"your address: {ident.address}\n"
              "next: session-recall share invite   (or join a peer's invite)")
        return 0

    ident = identity_mod.load(sdir)
    if ident is None:
        print("no share identity — run: session-recall share init <your-name>")
        return 1
    trust = TrustStore(sdir / "trust.json")

    if cmd in ("invite", "join", "complete"):
        transport = from_env(os.environ, identity=ident)
        if transport is None:
            print(_TRANSPORT_HINT)
            return 1
        try:
            if cmd == "invite":
                code = pairing.start_invite(ident, transport, sdir)
                print(f"one-time invite code (valid {pairing.INVITE_TTL_S // 60} min):\n\n"
                      f"    {code}\n\n"
                      "hand it to your peer out-of-band. After they run "
                      "`share join <code>`, run: session-recall share complete")
            elif cmd == "join":
                print(_sas_block(pairing.join(ident, transport, sdir, args.code)))
            else:
                print(_sas_block(pairing.complete_invite(ident, transport, sdir)))
        except PairingError as exc:
            print(f"pairing failed: {exc}")
            return 1
        return 0

    if cmd == "trust":
        cand = pairing.pending_peer(sdir)
        if cand is None:
            print("nothing to trust — finish a pairing (join/complete) first")
            return 1
        b = cand["bundle"]
        try:
            trust.add(Peer(name=b["name"], address=b["address"],
                           sign_pk=b["sign_pk"], box_pk=b["box_pk"]))
        except ValueError as exc:
            print(exc)
            return 1
        pairing.clear_pending_peer(sdir)
        print(f"trusted: {b['name']} ({b['address']}), mode=accept\n"
              "they can ask you questions once the answering service ships; "
              "revoke anytime: session-recall share revoke " + b["name"])
        return 0

    if cmd == "devices":
        peers = trust.peers(include_revoked=True)
        if not peers:
            print("no trusted peers yet")
            return 0
        for p in peers:
            mark = "REVOKED " if p.revoked else ""
            print(f"{mark}{p.name}  {p.address}  mode={p.mode}")
        return 0

    if cmd == "revoke":
        peer = trust.revoke(args.peer)
        if peer is None:
            print(f"no active peer matching {args.peer!r}")
            return 1
        print(f"revoked: {peer.name} ({peer.address}) — their envelopes now drop silently")
        return 0

    if cmd == "allow":
        if args.project is None:
            allowed = trust.allowed_projects()
            print("\n".join(allowed) if allowed else
                  "no shareable projects (default deny) — add one: "
                  "session-recall share allow <project>")
            return 0
        if args.remove:
            trust.disallow_project(args.project)
            print(f"no longer shareable: {args.project}")
        else:
            trust.allow_project(args.project)
            print(f"shareable: {args.project}")
        return 0

    return 1
