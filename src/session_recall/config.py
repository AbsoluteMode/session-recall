import json
import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

DATA_DIR = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "session-recall"
DB_PATH = DATA_DIR / "index.db"
SETTINGS_PATH = DATA_DIR / "settings.json"   # written by onboarding, human-editable
CLAUDE_PROJECTS = Path(
    os.environ.get("SESSION_RECALL_CLAUDE_PROJECTS")
    or (Path.home() / ".claude" / "projects")
).expanduser()
CODEX_HOME = Path(
    os.environ.get("CODEX_HOME") or (Path.home() / ".codex")
).expanduser()
CODEX_SESSIONS = CODEX_HOME / "sessions"
CODEX_ARCHIVED_SESSIONS = CODEX_HOME / "archived_sessions"


def _default_cursor_db(platform: str | None = None, env: dict | None = None) -> Path:
    """Cursor keeps its per-user state where its VS Code base does, which is a
    different directory on each OS — `%APPDATA%` on Windows, not `~/.config`,
    which is why a Windows install reported `sources: missing cursor` while the
    file sat there all along."""
    platform = sys.platform if platform is None else platform
    env = os.environ if env is None else env
    if platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif platform.startswith("win"):
        base = Path(env.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(env.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "Cursor" / "User" / "globalStorage" / "state.vscdb"


CURSOR_DB = Path(
    os.environ.get("SESSION_RECALL_CURSOR_DB") or _default_cursor_db()
).expanduser()

# Embedding provider — PLUGGABLE. A bundled ONNX model is the no-key default;
# Voyage remains the higher-quality hosted option. Any provider works: set the
# env vars below (e.g. provider=openai,
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
    # Bundled ONNX models (fastembed): no key, no server — the zero-setup
    # default. One per interaction language, because a 70MB English specialist
    # beats a 220MB generalist on English and loses everywhere else; the
    # language is asked once at onboarding (SESSION_RECALL_LANG or the
    # settings file), and "multi" safely covers everyone who never answered.
    "builtin-en": EmbedSettings(
        provider="builtin", model="BAAI/bge-small-en-v1.5", dim=384,
        base_url=None, send_dimensions=False,
        rerank_provider="none", rerank_model=None),
    "builtin-zh": EmbedSettings(
        provider="builtin", model="BAAI/bge-small-zh-v1.5", dim=512,
        base_url=None, send_dimensions=False,
        rerank_provider="none", rerank_model=None),
    "builtin-multi": EmbedSettings(
        provider="builtin",
        model="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        dim=384, base_url=None, send_dimensions=False,
        rerank_provider="none", rerank_model=None),
}


def user_lang(env: dict | None = None) -> str | None:
    """The interaction language, chosen once at project onboarding.

    With no explicit env dict this reads the live environment and falls back
    to the settings file; a passed-in env is taken as the whole world, so
    tests and callers stay hermetic."""
    live = env is None
    env = os.environ if live else env
    lang = (env.get("SESSION_RECALL_LANG") or "").strip().lower()
    if lang:
        return lang
    if live:
        try:
            stored = (json.loads(SETTINGS_PATH.read_text(encoding="utf-8")).get("lang") or "")
            return stored.strip().lower() or None
        except (OSError, ValueError):
            return None
    return None


def builtin_preset_for(lang: str | None) -> str:
    """en and zh get their specialist small model; every other answer — and
    no answer at all — gets the multilingual one, which is never wrong."""
    if lang and lang.startswith("en"):
        return "builtin-en"
    if lang and lang.startswith("zh"):
        return "builtin-zh"
    return "builtin-multi"

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

    `SESSION_RECALL_EMBED=<preset>` is the short path (`builtin` resolves to the
    right bundled model for the interaction language). With no preset: a Voyage
    key wins; a local server that is already running is next; and with nothing
    at all, the bundled ONNX model runs — out of the box always works, never a
    provider that is certain to reject us. Individual SESSION_RECALL_EMBED_*
    variables still win, so a preset never blocks a custom endpoint.
    """
    live = env is None
    env = os.environ if live else env
    probe = probe or _probe_local_server
    lang = user_lang(None if live else env)

    name = (env.get("SESSION_RECALL_EMBED") or "").strip().lower()
    if name == "builtin":
        name = builtin_preset_for(lang)
    if name:
        if name not in PRESETS:
            raise ValueError(
                f"unknown embed preset {name!r}; available: {', '.join(sorted(PRESETS))}")
        base = PRESETS[name]
    elif env.get("VOYAGE_API_KEY"):
        base = PRESETS["voyage"]
    else:
        detected = probe()
        base = PRESETS[detected] if detected else PRESETS[builtin_preset_for(lang)]

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


def embed_fingerprint() -> str:
    """Which embedding space vectors live in right now. Read at call time (not
    frozen above) so tests and long processes see configuration changes. The
    format is part of file signatures — change it and every file re-embeds."""
    return f"{EMBED_PROVIDER}/{EMBED_MODEL}/{EMBED_DIM}"
