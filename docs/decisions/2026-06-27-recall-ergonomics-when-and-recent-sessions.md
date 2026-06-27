# Recall ergonomics: human-readable timestamps + recent_sessions

**Date:** 2026-06-27 · **Branch:** `chore/recall-feedback-tails`

## Context

The same live-usage feedback that surfaced the grep crash (see
`2026-06-27-grep-resilient-to-deleted-transcripts.md`) also listed three ergonomics
rough edges (not bugs):

- `when` is surfaced as a raw epoch int — hard to tell "now" from "old".
- No "session tail / recent sessions" primitive, and the index freshness is not
  visible to the agent: to find the current state you had to sort hits by `when`
  and expand them by hand.
- A single thread is spread across several `session_id`s (resume creates new
  session files); the tool does not stitch them, so the arc was assembled by hand.

## Decision

- **`when_human`.** A `timefmt.humanize_ts(ts, now)` helper renders an epoch as
  `YYYY-MM-DD HH:MM UTC (Nx ago)`. The server enriches every ranked anchor
  (`recall_search`, `grep`) with a `when_human` field; the raw `when` epoch stays
  for sorting. `now` is injected (not read from the clock) so it is testable.
- **`recent_sessions(scope_cwd=None, limit=10)`** — a new read-only MCP tool. Lists
  sessions by most-recent activity (freshest first), each with `session_id`,
  `project`, `turns`, `last_activity` (epoch), `last_activity_human`, and a `label`
  (the session's first user prompt). Scoped via the existing `scope_cwd`/`repo_root`
  machinery.

This single tool covers two of the three asks directly — "freshest sessions /
session tail" and "how fresh is the index" (the top entry's `last_activity_human`
IS the effective freshness) — and substantially addresses the third: scoped,
recency-ordered sessions let the agent SEE the thread's sessions and reassemble the
arc without manual sorting.

## Why

These came from real friction, not speculation, so they are worth closing. But they
stay small and read-only: no new storage, no schema change, no reindex, no ambient
behavior — consistent with the project's "stay tool-based and simple" stance
(`2026-06-26-recall-stay-tool-based-simple.md`). `recent_sessions` is one cheap
GROUP BY over the existing `chunks` table plus a label lookup per row (limit is
small).

## What was tested

- `test_humanize_ts_absolute_and_relative` — empty for ts=0, relative buckets
  (just now / m / h / d) and the absolute UTC stamp.
- `test_recent_sessions_orders_scopes_and_labels` — freshest-first ordering, repo
  scoping, first-user-prompt label, turn count.
- `test_recall_search_enriches_with_human_timestamp` + delegation/scope test for the
  new tool at the server layer.
- Full suite: 64 passed, 0 regressions.
- Live index: `recent_sessions(scope_cwd=~/sidekey)` returned the three freshest
  sidekey sessions with readable "Nm ago" times, turn counts, and prompt labels.

## Rejected

- **Auto-stitching a thread across `session_id`s into one logical conversation** —
  would need a reliable cross-session link (how Claude Code chains resume sessions)
  and a merge heuristic; doing it blind risks wrong merges, which is worse than
  none. Deferred as a design item. `recent_sessions` covers the practical need
  (see the thread's sessions, ordered) without the risk.
- **A separate `index_freshness` / `index_status` tool** — folded into
  `recent_sessions` (its newest entry is the freshness signal) to avoid extra
  surface for the same information.
- **Changing `when` from epoch to a string** — would break sorting and is a
  breaking change to the anchor shape; added `when_human` alongside instead.

## Plugin tool namespacing fix (same pass)

While deduplicating the manual MCP registration against the installed plugin, the
manual user-scope `session-recall` MCP server was removed, leaving only the plugin's
bundled MCP. The plugin's `agents/recall.md` (`tools:` allowlist) and
`skills/session-recall/SKILL.md` referenced the tools by the **bare** form
`mcp__session-recall__<tool>` — which only resolved while the manual server existed.
In a plugin-only install the tools are namespaced
`mcp__plugin_session-recall_session-recall__<tool>`, so the bare names silently
resolved to nothing: a forcing test (asking the recall subagent about a topic only
in the transcripts) showed it returned NO session_id/uuid anchors and reconstructed
from git logs / memory files via `Read` instead — i.e. it had lost its recall tools.
External plugin-only users never had the manual server, so the subagent was broken
for them from the start.

Fix: `recall.md` lists tools in BOTH the plugin-namespaced and bare forms (unknown
names are silently ignored, so listing both resolves in either install mode);
`SKILL.md` refers to tools by bare name in prose (the agent maps to whatever it
has). Takes effect on the next Claude Code session (the plugin loads its agent
definition at startup).
