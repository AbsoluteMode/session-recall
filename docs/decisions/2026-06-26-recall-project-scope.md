# Recall can narrow the search to the current project (by cwd, not by project name)

**Date:** 2026-06-26

## Context

`recall_search`/`grep` searched globally across the entire session corpus. When working on
a specific repo this mixes in noise from other projects: a semantically similar
fragment from a foreign repo crowds the needed one out of top-k before the reranker even runs. We wanted
to optionally narrow the search to the current project without losing cross-project
recall.

The decision was made with a 4-engine council (Storm: claude+codex+glm+gemini) —
full consensus on every point, diverging only on the over-fetch multiplier.

## Decision

An optional `scope_cwd` on `recall_search` and `grep` (absent by default →
the previous global search). The agent passes its raw `cwd`; the server normalizes it
to the repo root and filters the **existing** `cwd` column by a bounded prefix. No
schema change and no reindexing.

- `scope.repo_root(cwd)` — strips the `/.claude/worktrees/<name>` suffix → all
  sessions of the repo (main + each worktree) collapse into one scope.
- `scope.scope_clause(column, root)` — a shared predicate for KNN and FTS (so the logic
  doesn't drift apart): `cwd = root OR cwd LIKE root||'/%' ESCAPE '\'`.
- KNN: vec0 cannot pre-filter on a joined column → over-fetch candidates
  (`clamp(n*30, 300, 2000)`, no more than total) + filter via JOIN, LIMIT n.
- FTS: JOIN `chunks` + the predicate before LIMIT (exact, no over-fetch).

## Why

- **The key = cwd, not the `project` name.** `_project_name` takes the last path segment
  → for a worktree it yields a garbage hash (`-Users-me-myrepo--claude-worktrees-...-a1b2c3`
  → `a1b2c3`), and almost all real work happens in a worktree. Filtering by name
  would miss ~90% of the repo's history and confuse collisions (`.../api` → `api`). `cwd` is
  a reliable field (the real absolute path from each turn).
- **Normalize with a string, not git.** `git rev-parse --show-toplevel` inside a
  worktree returns the worktree itself, not the parent, and fails on deleted/historical
  worktree paths stored in the index. A plain regex strip works every time.
- **The `/%` boundary is mandatory.** Without it `LIKE 'myrepo%'` would swallow the neighboring
  `myrepo-backend`. LIKE wildcards in root are escaped.
- **Default = global, narrowing is opt-in.** Purely additive, zero regression,
  cross-project recall stays the default. The server in user-scope doesn't know cwd itself, so
  the agent passes the string explicitly (the "scoped by default" policy lives in the
  subagent/SKILL prompt, not in the tool itself).
- **The server normalizes, the agent sends the raw cwd.** One clean testable function,
  no git dependency; the `recall` subagent without a shell cannot learn `pwd` itself —
  so the calling agent puts cwd into the dispatch.

## What was tested

- 17 new TDD tests (RED→GREEN), full suite 53 passed, 0 regressions.
- `repo_root`: plain path, worktree suffix, trailing slash, empty.
- `scope_clause`: None=no filter, exact-or-prefix, escaping `%`/`_`.
- store KNN/FTS: prefix filter + boundary `/repo` ≠ `/repo-backend`.
- retrieve: scoped excludes another repo; worktree cwd normalizes to the root; grep scoped.
- server: `scope_cwd` passthrough end-to-end on real data from two repos.
- **Hypothesis dropped:** we feared sqlite-vec would reject `MATCH`+`k` with an extra WHERE
  on a joined column (a two-step fallback was ready) — the installed version accepted
  the single-query JOIN, the fallback was not needed.

## Rejected

- Filtering by the `project` name — broken for worktrees, collisions.
- `git rev-parse` for the root — returns the worktree, fails on deleted paths.
- Per-repo / partitioned vec tables — overkill, the scan is cheap anyway.
- Denormalizing cwd into the FTS table — an extra rebuild, JOIN is simpler.
- Fixing the broken `_project_name` in this pass — cwd bypasses it; moved to a follow-up.
- Echoing the resolved scope in every result — not doing it yet (would break the output
  format); under-fill is covered by the "scoped too thin → re-run global" policy in the prompt.
- Server-side scope auto-detection — impossible in user-scope (cwd is not passed into MCP).

---
Implementation: `scope.py` (`repo_root` + `scope_clause`) + wiring into store/retrieve/server; tests `tests/test_scope.py`.
