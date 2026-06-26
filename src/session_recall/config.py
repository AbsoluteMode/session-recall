import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "session-recall"
DB_PATH = DATA_DIR / "index.db"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

# Embedding provider — PLUGGABLE. Voyage is the default (and the author's preference),
# but any provider works: set these env vars (e.g. provider=openai,
# model=text-embedding-3-large, dim=1024). Adding a provider = one branch in
# embed.make_embedder; the rest of the pipeline only sees the Embedder protocol.
# NB: changing provider or dim requires a fresh index — the vector table is dim-typed.
EMBED_PROVIDER = os.environ.get("SESSION_RECALL_EMBED_PROVIDER", "voyage")
EMBED_MODEL = os.environ.get("SESSION_RECALL_EMBED_MODEL", "voyage-4-large")
EMBED_DIM = int(os.environ.get("SESSION_RECALL_EMBED_DIM", "1024"))

# Reranker — OPTIONAL. Voyage rerank-2.5 by default; set provider=none to run on
# KNN + FTS only (not every embedding provider ships a reranker).
RERANK_PROVIDER = os.environ.get("SESSION_RECALL_RERANK_PROVIDER", "voyage")
RERANK_MODEL = os.environ.get("SESSION_RECALL_RERANK_MODEL", "rerank-2.5")
