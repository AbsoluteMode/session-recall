# grep is resilient to transcripts deleted after indexing

**Date:** 2026-06-27 · **Branch:** `fix/grep-resilient-deleted-transcripts`

## Context

Feedback from live usage (a different session, the tool rated 8/10):
global `grep` (without `session_id`) crashed with

```
[Errno 2] No such file or directory:
/Users/maxim/.claude/projects/-Users-maxim/98688231-0c4e-471d-aec2-a4ee74efda4f.jsonl
```

Hypothesis in the feedback: the path is "broken/truncated" (`-Users-maxim` instead of
`-Users-maxim-xyeta-trend-detection`), probably due to a space/special character in the session
path. Feedback conclusion: targeted grep by `session_id` is fine, global is unreliable.

## Decision

Wrap `_read_turns(file_path)` in `try/except OSError → return []`.
The fix is at the **source** (the single place where a file is opened from disk), not in the
`grep` loop. This covers all three disk-reading tools:

- `grep` (scan over all indexed paths) — a missing file yields `[]` turns →
  zero hits from it → the scan continues;
- `expand_around` / `step` (targeted drill-down) — a dead anchor degrades to
  `[]`, which is consistent with their own behavior on a not-found `uuid`.

**Index hygiene (complement, same pass).** `Store.prune_deleted()` at the start of
`index_corpus`: it walks `indexed_files`, and for those whose path is not on disk — drops their
chunks (`delete_file`: chunks + vec + fts) and the `indexed_files` row. `index_corpus`
only walks existing files, so a deleted one would never clean itself up.
Two layers of defense-in-depth: resilience keeps it from crashing **now**, prune removes
dead rows so they don't surface in `recall_search` either (where the text lives in the DB, and a
drill-down on such a hit would return nothing).

## Why

Diagnosis disproved the feedback hypothesis. Measurement against the real index:

- `~/.claude/projects/-Users-maxim/` **exists** — it is a valid project dir for
  sessions launched from home `/Users/maxim` (23 `.jsonl`). The path is not truncated, the encoding
  is correct.
- Of the **338** indexed `file_path` entries, exactly **one** is missing from disk — that very
  `98688231-…jsonl`. The file was **deleted after indexing**; its chunks remained in the DB and
  point at the vanished path.

The root cause is not path encoding, but the lack of resilience in the read layer to a
ghost file. Global grep walks EVERY indexed path, so it is
guaranteed to hit any deleted file and crash the whole scan on a single
`open()`. Session-scoped grep simply never touches that file — hence "targeted is fine,
global crashes".

`OSError` (rather than only `FileNotFoundError`) — also catches a file unreadable due to
permissions: for a scan, any unreadable file = skip, don't crash.

## What was tested

- RED test `test_grep_skips_missing_files_global`: two chunks, one → a live file with
  the needle, the other → a path that is not on disk; global grep must return the live
  hits and not crash. It failed with the same `FileNotFoundError` in `retrieve.py:81` as in
  prod → green after the fix.
- Live index: `grep("VOYAGE_API_KEY")` without `session_id` (walks all files,
  including the deleted one) → 688 hits instead of a crash.
- RED test `test_index_prunes_chunks_for_deleted_transcripts`: index 2 files,
  delete one from disk, re-index → its chunks (+ vec/fts + the `indexed_files` row) are
  pruned, the live file is intact. It failed (1 ≠ 0) before pruning → green.
- Live index: `prune_deleted()` → 1 file pruned (that very `98688231-…`); afterward —
  0 chunks point at a nonexistent path.
- Full suite: 60 passed, 0 regressions.

## Rejected

- **The "broken/truncated path" hypothesis (from the feedback)** — disproved by measurement: the dir
  is valid, the encoding is correct, the file was simply deleted.
- **Catching the error in the `grep` loop** — fixes only grep; `expand_around`/`step`
  would stay fragile on a dead anchor. The fix at the source covers them all.
- **Pruning as the SOLE fix** — it doesn't remove the crash by itself (a race: a file
  can be deleted between indexing and reading, in the same run). So pruning is
  taken as a **complement** to resilience (both in this pass), not a replacement.

Feedback ergonomics (not bugs, separate): there is no "session tail / recent
sessions" primitive; index freshness is not visible to the agent; a thread is spread across several `session_id`
(no stitching by worktree/thread); `when` is a raw epoch.
