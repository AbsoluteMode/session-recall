"""Project/repo scoping for recall search.

A user-scope MCP server can't know the caller's project, so the agent passes its
raw `cwd` and we normalize it here to a repo root, then filter the existing `cwd`
column by a boundary-safe prefix. No schema change, no reindex.

Worktrees nest UNDER the repo root (`<repo>/.claude/worktrees/<name>`), so
stripping that suffix collapses the main checkout and every worktree to ONE
scope — `project`-name derivation can't (it yields a junk hash per worktree).

# WHY: docs/decisions/2026-06-26-recall-project-scope.md
"""
import re

# Trailing `/.claude/worktrees/<name>` (optionally slash-terminated). Segment-
# anchored on `$` so it only strips a real worktree suffix, never mid-path.
_WORKTREE_SUFFIX = re.compile(r"/\.claude/worktrees/[^/]+/?$")


def repo_root(cwd: str) -> str:
    """Normalize a cwd to its parent repository root.

    Strips a trailing Claude-Code worktree segment so all of a repo's sessions
    (main + every worktree) share one scope; otherwise returns the path with any
    trailing slash removed. Pure string op — works on historical/deleted paths
    where a `git` call would fail.
    """
    if not cwd:
        return cwd
    return _WORKTREE_SUFFIX.sub("", cwd).rstrip("/")


def scope_clause(column: str, root: str | None) -> tuple[str, list[str]]:
    """Build a boundary-safe SQL predicate restricting `column` to `root`.

    Matches the root exactly OR any path strictly under it (`root/...`). The
    `/%` boundary is essential — a plain `LIKE 'root%'` would wrongly swallow a
    sibling like `myrepo-backend` next to `myrepo`. LIKE wildcards in the root
    are escaped. Returns `("", [])` when `root` is falsy (no filtering).
    """
    if not root:
        return "", []
    escaped = root.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%")
    sql = f"({column} = ? OR {column} LIKE ? ESCAPE '\\')"
    return sql, [root, escaped + "/%"]
