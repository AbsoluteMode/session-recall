import os
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

DATA_DIR = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "session-recall"
DB_PATH = DATA_DIR / "index.db"
CLAUDE_PROJECTS = Path(
    os.environ.get("SESSION_RECALL_CLAUDE_PROJECTS")
    or (Path.home() / ".claude" / "projects")
).expanduser()
CODEX_HOME = Path(
    os.environ.get("CODEX_HOME") or (Path.home() / ".codex")
).expanduser()
CODEX_SESSIONS = CODEX_HOME / "sessions"
CODEX_ARCHIVED_SESSIONS = CODEX_HOME / "archived_sessions"

# Embedding provider — PLUGGABLE. Voyage is the default (and the author's preference),
# but any provider works: set these env vars (e.g. provider=openai,
# model=text-embedding-3-large, dim=1024). Adding a provider = one branch in
# embed.make_embedder; the rest of the pipeline only sees the Embedder protocol.
# NB: provider/model changes are detected via the embed fingerprint baked into
# file signatures — the next `index` run re-embeds everything cleanly. Changing
# dim still requires a fresh index: the vector table is dim-typed.
@dataclass(frozen=True)
class EmbedSettings:
    provider: str
    model: str
    dim: int
    base_url: str | None
    send_dimensions: bool
    rerank_provider: str
    rerank_model: str | None


# One name per usable setup. A preset fills in endpoint, model, dimension and
# reranker together, because those four are not independent choices — picking a
# local model and leaving a cloud reranker configured just fails later, further away.
PRESETS: dict[str, EmbedSettings] = {
    "voyage": EmbedSettings(
        provider="voyage", model="voyage-4-large", dim=1024, base_url=None,
        send_dimensions=False, rerank_provider="voyage", rerank_model="rerank-2.5"),
    "openai": EmbedSettings(
        provider="openai", model="text-embedding-3-large", dim=1024, base_url=None,
        send_dimensions=True, rerank_provider="none", rerank_model=None),
    # Free and local. `nomic-embed-text` is Apache-2.0 and the most-pulled embedding
    # model in Ollama; deliberately not a stronger CC-BY-NC model, which would put
    # anyone indexing work history in breach without ever telling them.
    "ollama": EmbedSettings(
        provider="openai-compatible", model="nomic-embed-text", dim=768,
        base_url="http://127.0.0.1:11434/v1",
        send_dimensions=False, rerank_provider="none", rerank_model=None),
    "lmstudio": EmbedSettings(
        provider="openai-compatible", model="text-embedding-nomic-embed-text-v1.5",
        dim=768, base_url="http://127.0.0.1:1234/v1",
        send_dimensions=False, rerank_provider="none", rerank_model=None),
}

_PROBE_TIMEOUT_S = 0.3


def _probe_local_server() -> str | None:
    """Name of a local inference server that is already listening, else None.
    A TCP connect, not an HTTP call: this runs at import time when no key is set,
    and must cost nothing when nothing is there."""
    for name in ("ollama", "lmstudio"):
        url = urlparse(PRESETS[name].base_url)
        try:
            with socket.create_connection((url.hostname, url.port), _PROBE_TIMEOUT_S):
                return name
        except OSError:
            continue
    return None


def resolve_embed(env: dict | None = None, probe=None) -> EmbedSettings:
    """Turn the environment into one coherent embedding setup.

    `SESSION_RECALL_EMBED=<preset>` is the short path. With no preset and no Voyage
    key, a local server that is already running beats defaulting to a provider that
    is certain to reject us. Individual SESSION_RECALL_EMBED_* variables still win,
    so a preset never blocks a custom endpoint.
    """
    env = os.environ if env is None else env
    probe = probe or _probe_local_server

    name = (env.get("SESSION_RECALL_EMBED") or "").strip().lower()
    if name:
        if name not in PRESETS:
            raise ValueError(
                f"unknown embed preset {name!r}; available: {', '.join(sorted(PRESETS))}")
        base = PRESETS[name]
    elif env.get("VOYAGE_API_KEY"):
        base = PRESETS["voyage"]
    else:
        detected = probe()
        base = PRESETS[detected] if detected else PRESETS["voyage"]

    dim = env.get("SESSION_RECALL_EMBED_DIM")
    return EmbedSettings(
        provider=env.get("SESSION_RECALL_EMBED_PROVIDER", base.provider),
        model=env.get("SESSION_RECALL_EMBED_MODEL", base.model),
        dim=int(dim) if dim else base.dim,
        base_url=env.get("SESSION_RECALL_EMBED_BASE_URL", base.base_url),
        send_dimensions=base.send_dimensions,
        rerank_provider=env.get("SESSION_RECALL_RERANK_PROVIDER", base.rerank_provider),
        rerank_model=env.get("SESSION_RECALL_RERANK_MODEL", base.rerank_model),
    )


_EMBED = resolve_embed()

EMBED_PROVIDER = _EMBED.provider
EMBED_MODEL = _EMBED.model
EMBED_DIM = _EMBED.dim
EMBED_BASE_URL = _EMBED.base_url
EMBED_SEND_DIMENSIONS = _EMBED.send_dimensions

# Reranker — OPTIONAL. Voyage rerank-2.5 by default; set provider=none to run on
# KNN + FTS only (not every embedding provider ships a reranker).
RERANK_PROVIDER = _EMBED.rerank_provider
RERANK_MODEL = _EMBED.rerank_model
