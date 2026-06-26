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
    args = parser.parse_args(argv)

    store = Store(config.DB_PATH)
    if args.cmd == "index":
        n = index_corpus(store, make_embedder(), config.CLAUDE_PROJECTS)
        print(f"indexed {n} new chunks")
    elif args.cmd == "search":
        recall = Recall(store, make_embedder(), make_reranker())
        for a in recall.recall_search(args.query, k=args.k):
            print(f"[{a.score:.3f}] {a.project} {a.session_id} {a.role}: {a.snippet}")
    store.close()


if __name__ == "__main__":
    main()
