# Strip harness noise at extract-time + dedup at retrieve-time

**Date:** 2026-06-25 · **Branch:** `fix/extract-noise-dedup`

## Context

Search validation on 30 independent bug cases (mined by Codex from real
history, queries paraphrased without keywords) gave sessionHit@10 = 24/30 — a
working semantic recall — but exposed two quality defects:

- **Bug A — harness noise treated as "user speech".** `extract.py` indexed any
  string `user.content`. But Claude Code injects machine text into user turns:
  `<task-notification>`, `<system-reminder>`, `<command-message>`/`<command-name>`,
  `<local-command-stdout>`, `Caveat:` lines. Measurement: **1470 chunks = 4.6% of the index =
  22.5% of all user chunks.** In validation these blobs ranked #1 on real queries
  (a security question returned `<task-notification>` with score 0.625), crowding out the substance.
- **Bug B — duplicates in results.** `content_hash` was computed but not
  used for dedup; there is no diversity step at retrieve. Measurement: **4137 chunks (13%)** share a
  `content_hash` with another (resume sessions / sidechains / repeats). In validation two
  identical chunks took slots #1 and #2 (0.879 twice).

The decision was made by a Storm council (4/4 engines — claude/codex/glm/gemini — answered ok;
digest `/tmp/storm-fix.json`).

## Decision

- **Bug A → extract-time, "strip-and-keep-residual".** Cut out COMPLETE paired
  `<tag>…</tag>` blocks from an allowlist + anchor lines `Caveat: The messages below…` /
  `[Request interrupted…]`; if the residual is empty — drop the turn, otherwise index the
  residual. `content_hash` is computed over the cleaned text, while `byte_offset/byte_len`
  stay on the raw string (expand_around → source).
- **Bug B → retrieve-time collapse by `content_hash` BEFORE rerank**, over the full
  candidate pool; all rows stay in the DB (provenance). One representative per hash.
- **Root cause of duplicates → `index.py` delete-before-reinsert.** Before reindexing
  a changed file we call `store.delete_file(path)` — otherwise a growing transcript
  spawns duplicates on every re-scan. + `EXTRACTOR_VERSION` in the file signature: bumping
  the version invalidates all files → clean auto-reindex on future extractor changes.
- **Migration → full re-index** (drop DB → `session-recall index`).

## Why

- **Extract-time, not retrieve-time (A):** the reranker does NOT suppress noise (it was #1 @0.625),
  so it should not enter the candidate set at all; plus we don't pay Voyage to embed
  noise. Retrieve-time would still store/embed/rerank it.
- **Paired tags, not `if "<tag>" in text`:** a user who quoted `<system-reminder>`
  without a closing tag survives (≈0 false positives). `Caveat:` is anchored to the full
  harness sentence, not the bare word.
- **Retrieve-dedup, not index-skip (B):** the same text in different sessions is
  legitimate (the surrounding context differs) — index-skip would kill provenance.
- **No MMR for now:** the measured bug is EXACT duplicates (same hash), which hash collapse solves
  for free and without tuning; MMR (near-dup) adds a λ knob and the risk of dropping relevant content —
  deferred to v2 until the need is proven.
- **Full re-index, not in-place DELETE:** DELETE only removes standalone noise, but does not
  fix partially-boilerplate rows (their embedding was computed over dirty text → text↔vector drift);
  re-index has no drift, the index is one-shot under the leak-proof invariant, ~$1–3.

## What was tested

- 8 new unit tests (5 extract: a pure blob is dropped; appended-reminder → residual is
  kept; a quote of an unclosed tag survives; caveat+command is dropped; offset on
  the raw string. 1 store.delete_file. 1 index no-dup-rows. 1 retrieve content_hash
  collapse + provenance). Suite 31 passed / 1 deselected (live).
- Re-index + a re-run of the 30 queries (PROVEN before/after): noise chunks in
  the index **1470 → 6**; queries with a noise blob in top-10 **3 → 0** (#6/#8 top-1 switched
  from `<task-notification>` to real content); identical duplicates in top-k **4 → 0**;
  sessionHit@10 24 → 25. session-match barely moved (the blobs came from the CORRECT
  session — the metric already counted them as a hit), but the usefulness of the top chunk improved — which is the goal.

## Rejected

- A retrieve-time filter for A — still embeds/reranks the noise.
- Index-skip dedup for B — loses provenance.
- MMR now — overengineering for exact duplicates; v2.
- In-place DELETE as the migration — text↔vector drift on partially-boilerplate rows.

Branch: `fix/extract-noise-dedup`.
