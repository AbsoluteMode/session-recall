# Post-review hardening: bm25 rank, anchor resolution, resilient indexer, vector reuse

**Date:** 2026-07-02 · **Branch:** `fix/post-review-hardening`

## Context

A fresh code review (Fable, 2026-07-02) re-checked the Storm council findings of
2026-06-25 against the current code. Most critical items were already fixed
(delete-before-reinsert, noise filter, rerank fallback, project-from-cwd, scope).
Two live-proven bugs and two robustness gaps remained; all were confirmed by
probes against the real index (32 622 chunks / 421 files), not by inspection alone.

## Decision

1. **FTS ranks by bm25** (`ORDER BY rank` in both `fts()` branches). Proof of the
   bug: query with 1158 matches, `LIMIT` without ordering returned an arbitrary
   oldest slice (rowid order); with `rank` — the actual best keyword matches.
   Without this the hybrid's keyword arm was effectively noise.
2. **Anchor resolution works for every anchor `grep` can return.**
   `expand_around`/`step` resolved the transcript via `chunks` by uuid only;
   grep anchors pointing at never-chunked turns (tool_result-only, filtered
   boilerplate) exploded with a bare `KeyError` — reproduced live in-session.
   Now: uuid → session's chunk-bearing files → `indexed_files` by the
   `<session_id>.jsonl` name; if nothing resolves, a `LookupError` that names
   both ids and hints at reindexing.
3. **Indexer is per-file transactional and failure-isolated.** One transaction
   per file (delete + re-add + mark committed together, rollback on error), one
   bad file logs to stderr and is retried next run instead of aborting the other
   420. Side effect: no more fsync per chunk on backfill.
4. **Vectors are reused across re-indexes by `content_hash`.** Transcripts are
   append-only; the top live file is ~1400 chunks re-embedded on every
   SessionStart hook run before this. Now only genuinely new texts hit the
   embedding API (snapshot `{content_hash: embedding}` before `delete_file`,
   reuse blobs verbatim).
5. **grep scans `indexed_files`, not just chunk-bearing files** — a transcript
   whose every turn was filtered (pure boilerplate/tool traffic) is exactly the
   under-the-hood content grep exists for. Chunk-less files filter per-turn on
   raw `sessionId`/`cwd`.
6. Small: FTS-only hits carry `score: null` (not a fake `0.0` that reads as
   "irrelevant"), CLI gained `recent`/`grep`/`prune` (debugging without MCP),
   the plugin hook logs when the CLI is not on PATH instead of dying silently.

## Why

- bm25 and anchor resolution were the two remaining *correctness* holes: one
  starved retrieval quality invisibly, the other broke the advertised
  grep→expand workflow exactly on its target content.
- Per-file transactions bound the blast radius of any indexing failure to one
  file and one run — the hook runs unattended, so "log and retry" beats
  "crash and silently stale".
- Hash-based vector reuse is the cheapest possible incrementality: no schema
  change, no byte-offset bookkeeping, and it composes with the existing
  delete-before-reinsert invariant (rows are still rewritten; only the API call
  is skipped).

## What was tested

TDD throughout — every fix landed as a failing test first (12 new tests, suite
64 → 75 passed; red runs verified before each green). Live probes beforehand:
FTS ordering compared with/without `rank` on the real index; the grep→expand
KeyError reproduced against the running MCP server.

## Rejected

- **Tail-only indexing by `byte_offset`** for re-index cost — more bookkeeping,
  breaks on rewrites/compaction; hash reuse gives the same savings for free.
- **`check_same_thread=False`** (Storm's thread-safety worry) — verified
  unnecessary: FastMCP runs sync tools on the event loop
  (`call_fn_with_arg_validation` calls `fn(...)` directly, no `to_thread`).
- **Returning `[]` for unresolvable anchors** — hides "index is stale/empty"
  from the agent; an informative `LookupError` surfaces the cause in the tool
  error text.
- **AND/phrase FTS semantics, stop-words** — bm25's IDF already demotes
  frequent terms; keep OR-join recall wide, revisit only with an eval harness.

Commit: this branch, PR to `main`.
