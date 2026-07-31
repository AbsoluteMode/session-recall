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
    "transport disabled (SESSION_RECALL_RELAY_URL=none) — unset it to use the "
    "default relay, set it to your own relay, or set "
    "SESSION_RECALL_SHARE_TRANSPORT_DIR for a shared folder")


def add_parser(sub) -> None:
    shp = sub.add_parser("share", help="p2p recall sharing: pairing and trust")
    ssub = shp.add_subparsers(dest="share_cmd", required=True)
    ip = ssub.add_parser("init", help="create this device's share identity")
    ip.add_argument("name", help="how you introduce yourself to peers")
    ssub.add_parser("invite", help="start pairing; prints a one-time code")
    jp = ssub.add_parser("join", help="accept an invite code from a peer")
    jp.add_argument("code")
    ssub.add_parser("complete", help="finish pairing after the peer joined")
    tp = ssub.add_parser("trust",
                         help="confirm the SAS matched; enroll the peer under "
                              "a name YOU choose")
    tp.add_argument("petname", nargs="?",
                    help="what you will call them — unique, yours, required")
    ssub.add_parser("devices", help="list trusted peers")
    rp = ssub.add_parser("revoke", help="revoke a peer by petname or address")
    rp.add_argument("peer")
    ap = ssub.add_parser("allow",
                         help="grant a contact access to a project (no arg: list)")
    ap.add_argument("project", nargs="?")
    ap.add_argument("--to", dest="to", default=None, metavar="PETNAME",
                    help="which contact; `--to all` opens it to every contact")
    ap.add_argument("--remove", action="store_true")
    ssub.add_parser("pause", help="stop answering (questions wait at the relay)")
    ssub.add_parser("resume", help="resume answering after a pause")
    rlp = ssub.add_parser("relay", help="run the relay server (blind blob store)")
    rlp.add_argument("--port", type=int, default=8787)
    rlp.add_argument("--host", default="127.0.0.1",
                     help="bind address (default: localhost, for use behind TLS)")
    rlp.add_argument("--data", default=None,
                     help="storage dir (default: <data-dir>/share-relay)")
    svp = ssub.add_parser("serve", help="poll inbox, build candidate answers")
    svp.add_argument("--once", action="store_true", help="single sweep, then exit")
    svp.add_argument("--interval", type=int, default=30)
    ssub.add_parser("pending", help="list candidates waiting for approval")
    shp2 = ssub.add_parser("show", help="print one candidate in full")
    shp2.add_argument("id")
    tgp = ssub.add_parser("tg-setup", help="bind the Telegram approval bot")
    tgp.add_argument("--token", default=None,
                     help="bot token (default: $SESSION_RECALL_TG_TOKEN)")
    nfp = ssub.add_parser("notify", help="run the full loop: worker + TG approval + send")
    nfp.add_argument("--once", action="store_true")
    nfp.add_argument("--interval", type=int, default=5)
    akp = ssub.add_parser("ask", help="ask a trusted peer a question")
    akp.add_argument("peer", help="peer name or address")
    akp.add_argument("--doing", required=True, help="what you are working on")
    akp.add_argument("--problem", required=True, help="what broke, with symptoms")
    akp.add_argument("--want", required=True, help="what you want to know")
    rpp = ssub.add_parser("reply", help="continue a conversation you started")
    rpp.add_argument("thread")
    rpp.add_argument("text", nargs="+")
    ssub.add_parser("threads", help="list conversations")
    ssub.add_parser("fetch", help="collect answers peers have sent you")
    okp = ssub.add_parser("approve", help="approve a candidate locally (no TG)")
    okp.add_argument("id")
    okp.add_argument("version")
    dcp = ssub.add_parser("decline", help="decline a candidate locally (no TG)")
    dcp.add_argument("id")
    dcp.add_argument("reason", nargs="*")


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
        serve(args.port, Path(args.data) if args.data else config.DATA_DIR / "share-relay",
              host=args.host)
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

    if cmd == "serve":
        transport = from_env(os.environ, identity=ident)
        if transport is None:
            print(_TRANSPORT_HINT)
            return 1
        import time as _time
        from .. import config as _config
        from ..embed import make_embedder
        from ..rerank import make_reranker
        from ..retrieve import Recall
        from ..store import Store
        from .compose import make_composer
        from .envelope import ShareState
        from .worker import poll_once
        store = Store(_config.DB_PATH)
        recall = Recall(store, make_embedder(), make_reranker())
        searcher = lambda q, k: recall.recall_search(q, k=k)
        composer = make_composer()
        state = ShareState(sdir / "state.json")
        try:
            while True:
                for cand in poll_once(ident, trust, state, transport, searcher, sdir,
                                      composer=composer):
                    flags = f"  ⚠ {len(cand.findings)} secret flag(s)" if cand.findings else ""
                    print(f"[{cand.id} v{cand.version}] {cand.peer_name}: "
                          f"{cand.question[:80]}{flags}\n"
                          f"  review: session-recall share show {cand.id}")
                if args.once:
                    return 0
                _time.sleep(args.interval)
        finally:
            store.close()

    if cmd == "pending":
        from .worker import list_pending
        cands = list_pending(sdir)
        if not cands:
            print("no pending candidates")
            return 0
        for c in cands:
            flags = f"  ⚠ {len(c.findings)}" if c.findings else ""
            print(f"{c.id} v{c.version}  {c.peer_name}  {c.question[:70]}{flags}")
        return 0

    if cmd == "show":
        from .worker import load_candidate
        cand = load_candidate(sdir, args.id)
        if cand is None:
            print(f"no candidate {args.id!r}")
            return 1
        print(f"id: {cand.id}   version: {cand.version}   status: {cand.status}\n"
              f"from: {cand.peer_name} ({cand.peer_address})\n"
              f"question: {cand.question}")
        if cand.task:
            print(f'stated task (sender text, unverified): "{cand.task}"')
        if cand.findings:
            print("\n⚠ SECRET FLAGS — look closely before approving:")
            for f in cand.findings:
                print(f"  - {f['kind']}: {f['excerpt']}")
        print(f"\n--- candidate answer (v{cand.version}) ---\n{cand.text}")
        return 0

    if cmd == "threads":
        from . import thread as thread_mod
        convos = thread_mod.listing(sdir)
        if not convos:
            print("no conversations yet")
            return 0
        for t in convos:
            mark = "closed " if t.closed else ""
            last = t.turns[-1]["text"][:60] if t.turns else "(empty)"
            print(f"{mark}{t.id}  {t.peer_name}  {len(t.turns)} turns  {last}")
        return 0

    if cmd in ("ask", "reply", "fetch"):
        from . import thread as thread_mod
        from .ask import AskTooThin, validate
        from .envelope import ShareState, make_request, open_incoming
        transport = from_env(os.environ, identity=ident)
        if transport is None:
            print(_TRANSPORT_HINT)
            return 1

        if cmd == "reply":
            convo = thread_mod.load(sdir, args.thread)
            if convo is None:
                print(f"no conversation {args.thread!r} — see: session-recall share threads")
                return 1
            if convo.closed or convo.should_close():
                print(f"conversation {convo.id} is closed — start a new ask")
                return 1
            peer = trust.get_by_address(convo.peer_address)
            if peer is None:
                print(f"{convo.peer_name} is no longer trusted")
                return 1
            text = " ".join(args.text).strip()
            transport.post_mail(peer.address, make_request(
                ident, peer, text, thread=convo.id))
            convo.append("owner", text)
            thread_mod.save(sdir, convo)
            print(f"sent into {convo.id}; they approve before anything comes back")
            return 0

        if cmd == "ask":
            peer = trust.get(args.peer)
            if peer is None:
                print(f"no trusted peer {args.peer!r} — see: session-recall share devices")
                return 1
            try:
                body = validate(args.doing, args.problem, args.want)
            except AskTooThin as exc:
                print(f"that request is too thin to answer:\n{exc}")
                return 1
            transport.post_mail(peer.address, make_request(
                ident, peer, body["question"], task=body["task"],
                problem=body["problem"]))
            print(f"asked {peer.name}; they approve before anything comes back.\n"
                  "collect answers with: session-recall share fetch")
            return 0

        state = ShareState(sdir / "state.json")
        answers = 0
        for inbox in trust.inbox_addresses(ident):
            for raw in transport.fetch_mail(inbox):
                got = open_incoming(ident, trust, state, raw)
                if got is None or got.kind != "resp":
                    continue      # requests are the answering service's business
                answers += 1
                print(f"--- answer from {got.peer.label} ---\n"
                      f"{got.body.get('text', '')}\n")
        if not answers:
            print("no answers waiting")
        return 0

    if cmd == "tg-setup":
        from .telegram import TgApi, TgConfig, save_config
        token = args.token or os.environ.get("SESSION_RECALL_TG_TOKEN")
        if not token:
            print("need a bot token: --token or SESSION_RECALL_TG_TOKEN "
                  "(create one via @BotFather)")
            return 1
        api = TgApi(token)
        print("token saved. Now open your bot in Telegram and send it /start "
              "(waiting up to 2 min)…")
        cfg = TgConfig(token=token, chat_id=None)
        save_config(sdir, cfg)
        import time as _time
        deadline = _time.time() + 120
        offset = 0
        while _time.time() < deadline:
            for u in api.get_updates(offset, timeout=20):
                offset = max(offset, u.get("update_id", 0) + 1)
                msg = u.get("message") or {}
                if (msg.get("text") or "").strip() == "/start":
                    cfg.chat_id = msg["chat"]["id"]
                    cfg.offset = offset
                    save_config(sdir, cfg)
                    api.send_message(cfg.chat_id,
                                     "bound — approvals will arrive here")
                    print(f"bound to chat {cfg.chat_id}")
                    return 0
        print("no /start received — run tg-setup again")
        return 1

    if cmd in ("approve", "decline"):
        from . import approval
        transport = from_env(os.environ, identity=ident)
        if cmd == "approve":
            cand = approval.approve(sdir, args.id, args.version)
            if cand is None:
                print("not approved: wrong version or not pending "
                      f"(see: session-recall share show {args.id})")
                return 1
        else:
            cand = approval.reject(sdir, args.id, " ".join(args.reason))
            if cand is None:
                print(f"nothing pending under {args.id!r}")
                return 1
        if transport is None:
            print("decision recorded; no transport configured so nothing sent yet "
                  f"— {_TRANSPORT_HINT}")
            return 0
        sent = approval.dispatch(ident, trust, sdir, transport)
        for c in sent:
            print(f"{'sent' if c.status != 'rejected' else 'decline sent'}: "
                  f"{c.id} → {c.peer_name}")
        return 0

    if cmd == "notify":
        from .notify import NotifyLoop
        from .telegram import TgApi, load_config
        from .envelope import ShareState
        cfg = load_config(sdir)
        if cfg is None or cfg.chat_id is None:
            print("telegram not bound — run: session-recall share tg-setup")
            return 1
        transport = from_env(os.environ, identity=ident)
        if transport is None:
            print(_TRANSPORT_HINT)
            return 1
        from .. import config as _config
        from ..embed import make_embedder
        from ..rerank import make_reranker
        from ..retrieve import Recall
        from ..store import Store
        from .compose import make_composer
        store = Store(_config.DB_PATH)
        recall = Recall(store, make_embedder(), make_reranker())
        loop = NotifyLoop(TgApi(cfg.token), ident, trust,
                          ShareState(sdir / "state.json"), transport,
                          lambda q, k: recall.recall_search(q, k=k), sdir, cfg,
                          composer=make_composer())
        try:
            if args.once:
                stats = loop.tick()
                print(stats)
                return 0
            print("approval loop running (Ctrl-C to stop)")
            loop.run_forever(args.interval)
        finally:
            store.close()
        return 0

    if cmd == "trust":
        cand = pairing.pending_peer(sdir)
        if cand is None:
            print("nothing to trust — finish a pairing (join/complete) first")
            return 1
        b = cand["bundle"]
        # The petname is the owner's word against impersonation: the bundle's
        # name is whatever the peer typed, so the name everything else refers
        # to must be chosen HERE, by the human, at enrollment.
        petname = (args.petname or "").strip()
        if not petname:
            print(f'they introduce themselves as {b["name"]!r} — that is their '
                  "text, not a fact.\nenroll them under a name you choose: "
                  "session-recall share trust <petname>")
            return 1
        try:
            trust.add(Peer(name=b["name"], address=b["address"],
                           sign_pk=b["sign_pk"], box_pk=b["box_pk"],
                           petname=petname,
                           local_address=cand.get("local_address", "")))
        except ValueError as exc:
            print(exc)
            return 1
        pairing.clear_pending_peer(sdir)
        print(f"trusted: {petname} (introduces as {b['name']!r}), mode=accept\n"
              f"they see nothing until you grant scope: "
              f"session-recall share allow <project> --to {petname}\n"
              f"revoke anytime: session-recall share revoke {petname}")
        return 0

    if cmd == "devices":
        peers = trust.peers(include_revoked=True)
        if not peers:
            print("no trusted peers yet")
            return 0
        for p in peers:
            mark = "REVOKED " if p.revoked else ""
            scope = ", ".join(trust.projects_for(p)) or "(no scope — sees nothing)"
            claimed = f' "{p.name}"' if p.name != p.label else ""
            print(f"{mark}{p.label}{claimed}  {p.address}  mode={p.mode}  {scope}")
        return 0

    if cmd == "revoke":
        peer = trust.revoke(args.peer)
        if peer is None:
            print(f"no active peer matching {args.peer!r}")
            return 1
        print(f"revoked: {peer.label} ({peer.address}) — their channel is dead: "
              "we stop reading their inbox and their envelopes drop silently")
        return 0

    if cmd in ("pause", "resume"):
        trust.set_paused(cmd == "pause")
        print("paused — nothing is read or answered; questions wait at the relay"
              if cmd == "pause" else "resumed — answering again")
        return 0

    if cmd == "allow":
        if args.project is None:
            lines = [f"all contacts: {p}" for p in trust.allowed_projects()]
            for peer in trust.peers():
                lines += [f"{peer.label}: {p}" for p in peer.projects]
            print("\n".join(lines) if lines else
                  "no grants (default deny) — every answer comes back empty.\n"
                  "grant one: session-recall share allow <project> --to <petname>")
            return 0
        # Granting requires saying WHO. `--to all` is the everyone-bucket and
        # has to be typed out — opening a project to all contacts must never be
        # the accident of forgetting a flag.
        if args.to is None:
            print("say who: session-recall share allow "
                  f"{args.project} --to <petname>   (or --to all)")
            return 1
        peer = None
        if args.to != "all":
            peer = trust.get(args.to)
            if peer is None:
                print(f"no trusted peer {args.to!r} — see: session-recall share devices")
                return 1
        if args.remove:
            trust.disallow_project(args.project, peer)
            print(f"removed: {args.project} from "
                  f"{'all contacts' if peer is None else peer.label}")
        else:
            trust.allow_project(args.project, peer)
            print(f"{peer.label if peer else 'every contact'} may now see: "
                  f"{args.project}")
        return 0

    return 1
