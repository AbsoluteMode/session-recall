import argparse
from . import config
from .store import Store
from .embed import make_embedder
from .rerank import make_reranker
from .index import index_corpus
from .retrieve import Recall


def main(argv=None):
    parser = argparse.ArgumentParser(prog="session-recall")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("index")
    sp = sub.add_parser("search")
    sp.add_argument("query")
    sp.add_argument("-k", type=int, default=10)
    sp.add_argument("--scope", help="cwd to scope results to (repo root)")
    rp = sub.add_parser("recent")
    rp.add_argument("--scope")
    rp.add_argument("-n", type=int, default=10)
    gp = sub.add_parser("grep")
    gp.add_argument("pattern")
    gp.add_argument("--scope")
    gp.add_argument("--session")
    sub.add_parser("prune")  # drop index rows for transcripts deleted from disk
    args = parser.parse_args(argv)

    store = Store(config.DB_PATH)
    if args.cmd == "index":
        n = index_corpus(store, make_embedder(), config.CLAUDE_PROJECTS)
        print(f"indexed {n} new chunks")
    elif args.cmd == "search":
        recall = Recall(store, make_embedder(), make_reranker())
        for a in recall.recall_search(args.query, k=args.k, scope_cwd=args.scope):
            score = f"{a.score:.3f}" if a.score is not None else "fts"
            print(f"[{score}] {a.project} {a.session_id} {a.role}: {a.snippet}")
    elif args.cmd == "recent":
        recall = Recall(store, make_embedder(), make_reranker())
        for s in recall.recent_sessions(scope_cwd=args.scope, limit=args.n):
            print(f"{s['session_id']} {s['project']} {s['turns']}t "
                  f"{s['last_activity_human']} {s['label']}")
    elif args.cmd == "grep":
        recall = Recall(store, make_embedder(), make_reranker())
        for a in recall.grep(args.pattern, session_id=args.session, scope_cwd=args.scope):
            print(f"{a.session_id} {a.uuid} [{a.role}] {a.snippet}")
    elif args.cmd == "prune":
        print(f"pruned {store.prune_deleted()} deleted transcript(s)")
    store.close()


if __name__ == "__main__":
    main()
