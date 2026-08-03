import argparse
from pathlib import Path
from . import config
from .store import Store, corpus_summary
from .embed import make_embedder
from .rerank import make_reranker
from .index import index_corpus
from .retrieve import Recall
from .timefmt import date_range_to_epoch


def _add_date_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--date", help="one local calendar day (YYYY-MM-DD)")
    parser.add_argument("--start-date", help="inclusive local date (YYYY-MM-DD)")
    parser.add_argument("--end-date", help="inclusive local date (YYYY-MM-DD)")
    parser.add_argument(
        "--timezone", help="IANA timezone override (default: computer timezone)")


def _date_range(args, parser: argparse.ArgumentParser) -> tuple[int | None, int | None]:
    try:
        return date_range_to_epoch(
            args.start_date, args.end_date, args.timezone, on_date=args.date)
    except ValueError as exc:
        parser.error(str(exc))


_SOURCE_LABELS = {"claude": "Claude Code", "codex": "Codex"}
_ZONE_MARKS = {"GREEN": "ok  ", "AMBER": "warn", "RED": "FAIL"}


def _run_health(store: Store) -> int:
    """Print one row per dimension and a verdict. Exit code is non-zero on RED so a
    timer or monitor can act on it without parsing the text."""
    from .health import check_all

    roots = {"claude": config.CLAUDE_PROJECTS, "codex": config.CODEX_SESSIONS}
    transcripts = [p for root in (config.CLAUDE_PROJECTS, config.CODEX_SESSIONS,
                                  config.CODEX_ARCHIVED_SESSIONS)
                   if Path(root).is_dir() for p in Path(root).rglob("*.jsonl")]
    report = check_all(store, make_embedder(), roots, transcripts)

    width = max(len(d.name) for d in report.dimensions)
    for d in report.dimensions:
        print(f"[{_ZONE_MARKS[d.zone]}] {d.name.ljust(width)}  {d.detail}")
        if d.hint:
            print(f"{' ' * (width + 9)}→ {d.hint}")
    print(f"\nverdict: {report.verdict}"
          f" ({config.EMBED_PROVIDER}/{config.EMBED_MODEL}, index at {config.DB_PATH})")
    return 1 if report.verdict == "RED" else 0


def _print_corpus_summary(store: Store) -> None:
    """Say what the user now has. A raw chunk count reads as noise right after
    install — the interesting facts are how far back the memory reaches and that
    both engines feed it."""
    s = corpus_summary(store)
    if not s["sessions"]:
        return
    span = f" spanning {s['span_days']} days" if s["span_days"] else ""
    print(f"\nyour history: {s['sessions']} sessions{span}, "
          f"{s['chunks']:,} searchable fragments")
    if s["by_source"]:
        print("  " + " · ".join(
            f"{_SOURCE_LABELS.get(src, src)} {n}"
            for src, n in sorted(s["by_source"].items())))
    if s["top_projects"]:
        print("  busiest: " + ", ".join(p for p, _ in s["top_projects"]))
    print('\ntry: session-recall search "why did we choose"')


def main(argv=None):
    parser = argparse.ArgumentParser(prog="session-recall")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ip = sub.add_parser("index")
    ip.add_argument("--source", choices=("all", "claude", "codex", "cursor"),
                    default="all")
    sp = sub.add_parser("search")
    sp.add_argument("query")
    sp.add_argument("-k", type=int, default=10)
    sp.add_argument("--scope", help="cwd to scope results to (repo root)")
    sp.add_argument("--source", choices=("claude", "codex", "cursor"))
    _add_date_args(sp)
    rp = sub.add_parser("recent")
    rp.add_argument("--scope")
    rp.add_argument("-n", type=int, default=10)
    rp.add_argument("--source", choices=("claude", "codex", "cursor"))
    _add_date_args(rp)
    gp = sub.add_parser("grep")
    gp.add_argument("pattern")
    gp.add_argument("--scope")
    gp.add_argument("--session")
    gp.add_argument("--source", choices=("claude", "codex", "cursor"))
    gp.add_argument("--limit", type=int, default=100)
    _add_date_args(gp)
    pp = sub.add_parser("prune")  # drop rows for transcripts deleted from disk
    pp.add_argument("--source", choices=("claude", "codex", "cursor"))
    sub.add_parser("health")  # is recall actually working right now?
    from .share import cli as share_cli
    share_cli.add_parser(sub)
    from .metadocs import cli as metadocs_cli
    metadocs_cli.add_parser(sub)
    from . import onboarding
    onboarding.add_parser(sub)
    args = parser.parse_args(argv)

    if args.cmd == "share":  # no Store: share state lives outside the index DB
        return share_cli.run(args)
    if args.cmd == "metadocs":  # reads the index read-only via plain sqlite3
        return metadocs_cli.run(args)
    if args.cmd == "setup":  # indexes via a child process (fresh env resolve)
        return onboarding.run(args)

    store = Store(config.DB_PATH)
    if args.cmd == "health":
        return _run_health(store)
    if args.cmd == "index":
        claude_root = config.CLAUDE_PROJECTS if args.source in {"all", "claude"} else None
        codex_roots = ((config.CODEX_SESSIONS, config.CODEX_ARCHIVED_SESSIONS)
                       if args.source in {"all", "codex"} else ())
        embedder = make_embedder()
        n = index_corpus(
            store,
            embedder,
            claude_root,
            codex_dirs=codex_roots,
        )
        print(f"indexed {n} chunks from changed transcripts")
        if args.source in {"all", "cursor"}:
            from .cursor import index_cursor
            c = index_cursor(store, embedder)
            if c:
                print(f"indexed {c} cursor session(s)")
        # meta docs entries ride the same index (source="metadocs") whenever
        # the feature is configured; the SessionStart hook keeps them fresh
        if args.source == "all":
            from .metadocs import config as md_config
            md_cfg = md_config.load()
            if md_cfg is not None:
                from .metadocs.indexing import index_metadocs
                m = index_metadocs(store, embedder, Path(md_cfg.repo).expanduser())
                if m:
                    print(f"indexed {m} meta docs entr(y/ies)")
        _print_corpus_summary(store)
    elif args.cmd == "search":
        recall = Recall(store, make_embedder(), make_reranker())
        start_ts, end_ts = _date_range(args, parser)
        for a in recall.recall_search(
                args.query, k=args.k, scope_cwd=args.scope, source=args.source,
                start_ts=start_ts, end_ts=end_ts):
            score = f"{a.score:.3f}" if a.score is not None else "fts"
            print(f"[{score}] {a.source} {a.project} {a.session_id} {a.role}: {a.snippet}")
    elif args.cmd == "recent":
        recall = Recall(store, make_embedder(), make_reranker())
        start_ts, end_ts = _date_range(args, parser)
        for s in recall.recent_sessions(
                scope_cwd=args.scope, limit=args.n, source=args.source,
                start_ts=start_ts, end_ts=end_ts):
            print(f"{s['source']} {s['session_id']} {s['project']} {s['turns']}t "
                  f"{s['last_activity_human']} {s['label']}")
    elif args.cmd == "grep":
        recall = Recall(store, make_embedder(), make_reranker())
        start_ts, end_ts = _date_range(args, parser)
        for a in recall.grep(
                args.pattern, session_id=args.session, scope_cwd=args.scope,
                source=args.source, limit=args.limit,
                start_ts=start_ts, end_ts=end_ts):
            print(f"{a.source} {a.session_id} {a.uuid} [{a.role}] {a.snippet}")
    elif args.cmd == "prune":
        print(f"pruned {store.prune_deleted(source=args.source)} deleted transcript(s)")
    store.close()


if __name__ == "__main__":
    main()
