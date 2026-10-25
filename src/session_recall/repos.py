"""Which repositories a session actually worked on, and who started it.

`chunks.project` is the folder the session was LAUNCHED in. That is often not
the project: a session opened in one repo's worktree edits another repo by
absolute path, a desktop scratch folder works on a real repo, a symlinked
alias (`~/exodia -> whytap`) splits one repo into two names. A question like
"what happened in harmonia" then misses the sessions that did the work.

The evidence is already in every transcript: the tool calls. Every path a
call names — an Edit's file_path, a `cd`, a `git -C`, a Codex workdir — is
resolved to the git repository that owns it, and the session is attributed
to each repo it touched often enough. Only tool INPUTS count: a result that
merely lists another repo's files is not work on that repo.

Resolution walks the real filesystem up to the nearest `.git` (a worktree's
`.git` file points back at its main checkout), then follows symlinks, so a
worktree, its main checkout and a symlinked alias all become one root.
Paths that no longer exist fall back to the nearest existing ancestor; a
session's own cwd that is not a git repo still counts under its plain name,
so a session is never left with less attribution than `project` gave it.

# WHY: docs/decisions/2026-10-25-recall-for-registrar.md
"""

import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .scope import repo_root
from .transcripts import iter_transcript_events

# Bump when attribution logic changes: baked into the per-file scan signature,
# so every transcript is re-scanned (no re-embedding — scans are text-only).
REPO_SCAN_VERSION = 1

# A repo other than the session's own cwd must be named by at least this many
# tool calls. One `cat` of a neighbour's README is a glance, not work on it.
MIN_TOUCHES = 3

# An absolute (or ~-relative) path inside free text: a JSON string, a shell
# command, JS code. Stops at quotes, shell metacharacters and whitespace.
_PATH = re.compile(r"(?<![\w.~/-])(~?/[^\s'\"`;|&<>(){}\[\],*?$\\]+)")

# Never a project: system and tool-state locations. Plugin caches and
# Claude/Codex state live in hidden home dirs and are often git clones, so
# reading one SKILL.md must not attribute the session to a plugin.
_SYSTEM_PREFIXES = ("/tmp", "/private", "/var", "/etc", "/usr", "/opt", "/bin",
                    "/sbin", "/dev", "/System", "/Library", "/Applications",
                    "/proc", "/sys")

# Codex keeps worktrees at ~/.codex/worktrees/<id>/<repo-name> and deletes
# them after the thread; a gone one has no `.git` file left to follow.
_CODEX_WORKTREE = re.compile(r"/\.codex/worktrees/[^/]+/(?P<name>[^/]+)(?P<rest>/.*)?$")

# Who opened a session. A transcript says it with metadata, not with words:
# Claude's `entrypoint` (sdk-* = driven by a program), Codex's `originator`
# and `source` (exec = codex exec; "Claude Code" = a Codex thread Claude
# opened). A side session is opened by another session with a written brief;
# a Codex automation wakes up on a heartbeat.
_AGENT_BRIEF = "Background (from the session that started this one)"
_HEARTBEAT = "<heartbeat>"


@dataclass
class SessionFacts:
    """What one transcript says about one session."""
    cwd: str = ""
    touches: Counter = field(default_factory=Counter)
    started_by: str = ""


class RepoResolver:
    """Path -> owning repo root, memoized per directory.

    One resolver serves a whole index run: thousands of tool calls name the
    same few directories, and each lookup is a handful of stat() calls."""

    def __init__(self, home: str | None = None):
        self.home = (home or os.path.expanduser("~")).rstrip("/")
        self._cache: dict[str, str | None] = {}

    @staticmethod
    def _system(path: str) -> bool:
        return any(path == p or path.startswith(p + "/") for p in _SYSTEM_PREFIXES)

    def _ignored(self, path: str) -> bool:
        if path == self.home:
            return True
        if path.startswith(self.home + "/"):
            first = path[len(self.home) + 1:].split("/", 1)[0]
            return first.startswith(".") or first == "Library"
        return self._system(path)

    @staticmethod
    def _main_checkout(dot_git: Path) -> Path | None:
        """A worktree's `.git` is a file: `gitdir: <main>/.git/worktrees/<n>`."""
        try:
            text = dot_git.read_text(errors="replace")
        except OSError:
            return None
        if not text.startswith("gitdir:"):
            return None
        gitdir = text[len("gitdir:"):].strip()
        marker = "/.git/worktrees/"
        if marker in gitdir:
            return Path(gitdir.split(marker, 1)[0])
        return None

    def _revive_codex_worktree(self, path: str) -> str:
        """A deleted Codex worktree -> the same-named checkout in home, if any."""
        found = _CODEX_WORKTREE.search(path)
        if not found or os.path.exists(path[:found.end("name")]):
            return path
        main = os.path.join(self.home, found["name"])
        if not os.path.exists(os.path.join(main, ".git")):
            return path
        return main + (found["rest"] or "")

    def repo_of(self, path: str) -> str | None:
        """The git repo that owns `path` (a file or a directory), or None."""
        if not path:
            return None
        path = os.path.expanduser(path)
        # "//host/x" is the tail of a URL ("https://host/x"), not a path —
        # and POSIX normpath keeps the double slash, so dirname never ends.
        if not path.startswith("/") or path.startswith("//"):
            return None
        # Worktree folders collapse by name first: a deleted worktree no
        # longer has the `.git` file that would lead back to its checkout.
        path = os.path.normpath(repo_root(path) or "/")
        # Only system locations are skipped up front. Hidden home folders are
        # judged by the repo they lead to: a Codex worktree under
        # ~/.codex/worktrees/ is real work on its main checkout, a plugin
        # cache cloned under ~/.claude/ is not.
        if path == "/" or path == self.home:
            return None
        if not path.startswith(self.home + "/") and self._system(path):
            return None
        path = self._revive_codex_worktree(path)
        visited: list[str] = []
        current = path
        found: str | None = None
        while current and current != "/":
            if current in self._cache:
                found = self._cache[current]
                break
            visited.append(current)
            if current == self.home:
                break
            dot_git = Path(current) / ".git"
            try:
                if dot_git.is_dir():
                    found = current
                    break
                if dot_git.is_file():
                    main = self._main_checkout(dot_git)
                    found = str(main) if main else current
                    break
            except OSError:
                pass
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        if found:
            found = os.path.realpath(found)
            if self._ignored(found):
                found = None
        for directory in visited:
            self._cache[directory] = found
        return found

    def cwd_root(self, cwd: str) -> str | None:
        """A session's own folder: its repo, else the folder itself by name.

        A cwd that is not a git checkout (or no longer exists, as on a hub
        that indexes someone else's paths) still names the session's place —
        dropping it would attribute less than the old `project` label did."""
        if not cwd:
            return None
        found = self.repo_of(cwd)
        if found:
            return found
        name = repo_root(os.path.expanduser(cwd))
        if not name.startswith("/") or self._ignored(name):
            return None
        return name


def owning_repo(path: str) -> str:
    """The repo a search scope means: the checkout that contains `path`
    (an agent often runs from `<repo>/src`), else the path itself.

    No attribution filters here — the caller named this place explicitly."""
    current = os.path.normpath(repo_root(os.path.expanduser(path)) or "/")
    while current.startswith("/") and current != "/":
        dot_git = Path(current) / ".git"
        try:
            if dot_git.is_dir():
                return os.path.realpath(current)
            if dot_git.is_file():
                main = RepoResolver._main_checkout(dot_git)
                return os.path.realpath(main or current)
        except OSError:
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return os.path.realpath(path)


def _tool_inputs(event) -> list[str]:
    """The text of every tool call this event makes (inputs only)."""
    obj = event.obj
    if event.source == "claude":
        message = obj.get("message") or {}
        content = message.get("content")
        if obj.get("type") != "assistant" or not isinstance(content, list):
            return []
        out = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                out.append(_flatten(block.get("input")))
        return out
    if event.source == "codex":
        payload = obj.get("payload")
        if obj.get("type") != "response_item" or not isinstance(payload, dict):
            return []
        if payload.get("type") not in {"function_call", "custom_tool_call",
                                       "local_shell_call"}:
            return []
        return [_flatten(payload.get("arguments", payload.get("input",
                                                         payload.get("action"))))]
    return []


def _flatten(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(_flatten(v) for v in value.values())
    if isinstance(value, list):
        return "\n".join(_flatten(v) for v in value)
    return ""


def _first_user_text(event) -> str | None:
    obj = event.obj
    if event.source == "claude":
        message = obj.get("message") or {}
        if obj.get("type") == "user" and isinstance(message.get("content"), str):
            from .extract import _clean_user_text
            return _clean_user_text(message["content"])
        return None
    if event.source == "codex" and event.role == "user" and event.content.strip():
        return event.content
    return None


def classify_starter(source: str, entrypoint: str, originator: str,
                     codex_source: str, first_text: str) -> str:
    """'user' | 'agent' | 'automation' — who opened the session.

    Metadata first (it cannot be faked by a prompt), then the two written
    shapes another program leaves as the first message."""
    text = (first_text or "").lstrip()
    if text.startswith(_HEARTBEAT):
        return "automation"
    if text.startswith(_AGENT_BRIEF):
        return "agent"
    if source == "claude" and entrypoint.startswith("sdk"):
        return "agent"
    if source == "codex" and (codex_source == "exec" or originator == "Claude Code"):
        return "agent"
    return "user"


def scan_file(path: str, source: str,
              resolver: RepoResolver) -> dict[str, SessionFacts]:
    """session_id -> facts, streamed in one pass over a transcript."""
    facts: dict[str, SessionFacts] = {}
    starters: dict[str, dict] = {}
    for event in iter_transcript_events(path, source=source):
        sid = event.session_id
        if not sid:
            continue
        session = facts.setdefault(sid, SessionFacts())
        if event.cwd and not session.cwd:
            session.cwd = event.cwd
        meta = starters.setdefault(sid, {"entrypoint": "", "originator": "",
                                         "codex_source": "", "text": None})
        obj = event.obj
        if source == "claude" and not meta["entrypoint"] and obj.get("entrypoint"):
            meta["entrypoint"] = str(obj["entrypoint"])
        if source == "codex" and obj.get("type") == "session_meta" \
                and not meta["originator"]:
            payload = obj.get("payload") or {}
            meta["originator"] = str(payload.get("originator") or "")
            raw_source = payload.get("source")
            meta["codex_source"] = raw_source if isinstance(raw_source, str) else ""
        if meta["text"] is None:
            text = _first_user_text(event)
            if text:
                meta["text"] = text
        for text in _tool_inputs(event):
            roots = {resolver.repo_of(p) for p in _PATH.findall(text)}
            roots.discard(None)
            if not roots:
                # A call that names no absolute path runs where the session
                # is (or its Codex workdir, which _PATH already caught).
                here = resolver.cwd_root(event.cwd)
                roots = {here} if here else set()
            for root in roots:
                session.touches[root] += 1
    for sid, session in facts.items():
        meta = starters[sid]
        session.started_by = classify_starter(
            source, meta["entrypoint"], meta["originator"], meta["codex_source"],
            meta["text"] or "")
    return facts


def attributed_repos(session: SessionFacts,
                     resolver: RepoResolver) -> list[tuple[str, int]]:
    """(root, touches) the session is attributed to, most-touched first.

    The session's own cwd repo always counts (a pure discussion still belongs
    where it was held); any other repo needs MIN_TOUCHES tool calls."""
    home = resolver.cwd_root(session.cwd)
    out = [(root, n) for root, n in session.touches.items()
           if n >= MIN_TOUCHES or root == home]
    if home and home not in session.touches:
        out.append((home, 0))
    out.sort(key=lambda item: (-item[1], item[0]))
    return out
