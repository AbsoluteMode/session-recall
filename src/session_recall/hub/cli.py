"""`session-recall hub …` — the team hub, from both sides.

Operator commands run the service, issue and revoke member keys, refresh the
masking map and index what has arrived. Member commands are just `join`,
`push`, `status` and `leave`. They share a namespace but not an
implementation: the member half lives in `hub/client.py` and needs nothing
from the server module.

Key issuance is intentionally CLI-only and never exposed over HTTP: minting
credentials is an operator action at a shell, not a request an agent or a
compromised member key can make.
"""

import argparse
import json
import os
from http.server import ThreadingHTTPServer
from pathlib import Path

DEFAULT_DIR = "/var/lib/claude-recall"
DEFAULT_PORT = 8788


def hub_dir(args) -> Path:
    return Path(getattr(args, "dir", None)
                or os.environ.get("SESSION_RECALL_HUB_DIR")
                or DEFAULT_DIR).expanduser()


def add_parser(sub) -> None:
    hp = sub.add_parser("hub", help="team hub: serve, keys, indexing")
    hp.add_argument("--dir", default=None,
                    help=f"hub data directory (default {DEFAULT_DIR}, "
                         f"or SESSION_RECALL_HUB_DIR)")
    hsub = hp.add_subparsers(dest="hub_cmd", required=True)

    sp = hsub.add_parser("serve", help="run the hub HTTP service")
    sp.add_argument("--host", default="127.0.0.1",
                    help="bind address; keep it on localhost behind a TLS proxy")
    sp.add_argument("--port", type=int, default=DEFAULT_PORT)

    kp = hsub.add_parser("key", help="member keys")
    ksub = kp.add_subparsers(dest="key_cmd", required=True)
    ki = ksub.add_parser("issue", help="mint a key for a member")
    ki.add_argument("owner", help="short name: lowercase letters, digits, dashes")
    ki.add_argument("--note", default="", help="free text, e.g. which laptop")
    ksub.add_parser("list", help="issued keys (never shows a usable key)")
    kr = ksub.add_parser("revoke", help="revoke by key id or by member name")
    kr.add_argument("selector")

    hsub.add_parser("index", help="index everything members have uploaded")

    # --- member side ---------------------------------------------------
    jp = hsub.add_parser("join", help="connect this machine to a team hub")
    jp.add_argument("url", help="https://recall.example.com")
    jp.add_argument("key", help="the key your operator handed you")
    jp.add_argument("--yes", action="store_true",
                    help="accept the disclosure without the interactive prompt")
    pp = hsub.add_parser("push", help="upload new transcript bytes to the hub")
    pp.add_argument("--quiet", action="store_true", help="only print the summary")
    ap = hsub.add_parser("ask", help="ask the team's history a question")
    ap.add_argument("question")
    ap.add_argument("-k", type=int, default=12, help="fragments to retrieve")
    ap.add_argument("--scope", default=None, help="limit to one repo (cwd)")
    hsub.add_parser("status", help="what this machine sends, and where")
    hsub.add_parser("leave", help="stop sending (uploaded history stays on the hub)")

    secp = hsub.add_parser("secrets", help="the Doppler masking map")
    secsub = secp.add_subparsers(dest="secrets_cmd", required=True)
    secsub.add_parser("refresh", help="rebuild the map from Doppler")
    secsub.add_parser("status", help="how many secrets are masked, and when")


def _print_answer(result: dict) -> None:
    print(result["answer"])
    if result.get("degraded"):
        print(f"\n⚠ поиск деградировал: {result['degraded']}")
    if not result.get("composed"):
        print("\n(модель не подключена — показаны найденные фрагменты)")
    seen = []
    for source in result.get("sources", []):
        label = f"{source.get('owner') or '?'} · {source.get('project') or '?'}"
        if label not in seen:
            seen.append(label)
    if seen:
        print("\nисточники: " + ", ".join(seen))


def _member(args) -> int | None:
    """Member-side commands. Return None when `args` is not one of them."""
    from .client import CONFIG_PATH, CONSENT, HubConfig, HubError, join, push

    if args.hub_cmd == "join":
        print(CONSENT)
        if not args.yes:
            try:
                answer = input("Подключить эту машину к общему индексу? [y/N] ")
            except EOFError:
                answer = ""
            if answer.strip().lower() not in ("y", "yes", "д", "да"):
                print("отменено — ничего не отправлено")
                return 1
        try:
            cfg = join(args.url, args.key)
        except HubError as failure:
            print(f"не подключились: {failure}")
            return 1
        print(f"подключено к {cfg.url}\n"
              f"дальше: session-recall hub push (первая заливка — вся история)")
        return 0

    if args.hub_cmd == "push":
        cfg = HubConfig.load()
        if cfg is None:
            print("эта машина не подключена — сначала `session-recall hub join`")
            return 1
        try:
            stats = push(cfg, progress=None if args.quiet else print)
        except HubError as failure:
            print(f"заливка не удалась: {failure}")
            return 1
        print(f"файлов отправлено: {stats['files']}, "
              f"байт: {stats['uploaded_bytes']}, "
              f"вырезано секретов: {stats['redacted']}, "
              f"пропущено: {stats['skipped']}, ошибок: {stats['failed']}")
        return 1 if stats["failed"] else 0

    if args.hub_cmd == "ask":
        cfg = HubConfig.load()
        if cfg is None:
            return None      # no membership: fall through to the operator path
        from .remote import RemoteRecall
        try:
            result = RemoteRecall(cfg).ask(args.question, k=args.k,
                                           scope_cwd=args.scope)
        except HubError as failure:
            print(f"не удалось спросить: {failure}")
            return 1
        _print_answer(result)
        return 0

    if args.hub_cmd == "status":
        cfg = HubConfig.load()
        if cfg is None:
            print("не подключено к хабу (solo-режим: индекс остаётся локальным)")
            return 0
        print(json.dumps({"url": cfg.url, "config": str(CONFIG_PATH),
                          "consented": cfg.consented}, indent=2))
        return 0

    if args.hub_cmd == "leave":
        if CONFIG_PATH.exists():
            CONFIG_PATH.unlink()
            print("отключено — эта машина больше ничего не отправляет.\n"
                  "Уже загруженное остаётся на сервере: попроси оператора удалить.")
        else:
            print("эта машина и так не подключена")
        return 0
    return None


def run(args) -> int:
    member = _member(args)
    if member is not None:
        return member

    from .app import Hub, make_handler
    from .auth import KeyStore

    root = hub_dir(args)
    hub = Hub(root)

    if args.hub_cmd == "serve":
        httpd = ThreadingHTTPServer((args.host, args.port), make_handler(hub))
        masked = len(hub.secret_map.labels)
        print(f"claude-recall on {args.host}:{args.port}, data in {root}")
        print(f"masking {masked} known secret(s)"
              if masked else
              "WARNING: no masking map — run `session-recall hub secrets refresh` "
              "before enrolling members, or credentials land in the index")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
        return 0

    if args.hub_cmd == "key":
        keys = KeyStore(root / "keys.json")
        if args.key_cmd == "issue":
            key = keys.issue(args.owner, args.note)
            print(key)
            print("\nHand this to the member ONCE, over a channel you trust. "
                  "It is not stored and cannot be shown again.")
            return 0
        if args.key_cmd == "list":
            for row in keys.listing():
                state = f"revoked" if row.get("revoked") else "active"
                note = f"  {row['note']}" if row.get("note") else ""
                print(f"{row['id']}  {row['owner']:<16} {state}{note}")
            return 0
        if args.key_cmd == "revoke":
            count = keys.revoke(args.selector)
            print(f"revoked {count} key(s)")
            return 0 if count else 1

    if args.hub_cmd == "index":
        from .indexer import index_all
        print(json.dumps(index_all(hub), indent=2))
        return 0

    if args.hub_cmd == "ask":
        # Reached only when this machine has not joined a hub — i.e. the
        # operator asking their own server directly.
        from . import ask as hub_ask
        _print_answer(hub_ask.answer(hub, args.question, k=args.k,
                                     scope_cwd=args.scope,
                                     composer=hub.composer))
        return 0

    if args.hub_cmd == "secrets":
        from .masking import SecretMap, collect_from_doppler
        if args.secrets_cmd == "refresh":
            try:
                entries = collect_from_doppler()
            except Exception as failure:                   # noqa: BLE001
                print(f"doppler unavailable: {failure}\n"
                      f"keeping the previous map "
                      f"({len(hub.secret_map.labels)} secrets)")
                return 1
            previous = SecretMap.load(hub.secrets_path)
            # Reuse the salt so the map stays comparable across refreshes and
            # an operator can diff counts without every hash changing.
            fresh = SecretMap.build(entries, salt=previous.salt or None)
            fresh.save(hub.secrets_path)
            print(f"read {len(entries)} secret(s) from Doppler, "
                  f"masking {len(fresh.labels)} of them "
                  f"(short or low-entropy values are skipped on purpose)")
            return 0
        if args.secrets_cmd == "status":
            smap = hub.secret_map
            print(json.dumps({"masked_secrets": len(smap.labels),
                              "updated": smap.updated}, indent=2))
            return 0

    raise argparse.ArgumentTypeError(f"unknown hub command: {args.hub_cmd}")
