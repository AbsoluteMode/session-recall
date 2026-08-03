# Cursor as a durable raw recall source

Date: 2026-08-03

## Context

Cursor stores all workspaces in one private SQLite database rather than one
append-only transcript per session.  The first adapter indexed visible bubble
text under virtual paths (`cursor:<composerId>`).  Semantic search and recent
sessions worked, but the normal recall workflow did not: `expand_around`,
`step`, and `grep` opened transcript files, while a virtual path has no file to
open.  The private schema can also change independently of session-recall.

## Decision

On each Cursor index pass:

1. Read one transactionally consistent SQLite backup (never copy a live db and
   WAL sidecar separately).
2. Discover schema capabilities.  Prefer `composerHeaders`; fall back to
   `composerData:*` keys when the catalog table is absent.
3. Preserve every available bubble, including unknown/tool/reasoning shapes,
   in a sanitized normalized JSONL session snapshot under
   `<data-dir>/cursor-transcripts/`.
4. Name snapshots by hashes of the composer id and content.  A changed session
   gets a new file, so a failed DB transaction cannot make old chunk rows point
   at new bytes.
5. Embed only non-empty user/assistant surface text.  Raw tool/reasoning data is
   available solely through explicit expand/step/grep.
6. Point chunks and `indexed_files` at the physical snapshot.  After a
   successful migration, delete the old `cursor:<id>` row without re-embedding
   unchanged surface text.

Snapshots deliberately survive Cursor being closed or uninstalled.  Deleting a
session from an available Cursor catalog removes its indexed rows and snapshot.
An absent database is treated as temporarily unavailable and preserves the last
good history.

## Native Cursor host integration

Cursor is both a history source and an MCP host. The repository therefore ships
its native plugin artifacts alongside the Claude and Codex manifests:

- `.cursor-plugin/plugin.json` and `.cursor-plugin/marketplace.json`;
- Cursor's wrapped `mcp.json` shape;
- flat, lower-camel `hooks/hooks-cursor.json` with `version: 1` and a
  `sessionStart` refresh;
- the shared skills, commands, and recall subagent.

The Claude hook file is not reused: its `SessionStart`/nested-`hooks` wire shape
is a different protocol even though it launches the same incremental indexer.
Cross-host manifest tests lock component paths, version alignment, and the
native MCP/hook shapes. The Cursor manifest and marketplace also validate
against the schemas from the official `cursor/plugins` repository.

## Embedding-space invariant

Claude, Codex, Cursor, and meta docs share one vec0 table.  The global
`meta.embed_fp` marker is therefore derived from **all** `indexed_files`
signatures after every producer pass.  A source-selective or failed pass records
`mixed`; search disables KNN until every row agrees.  `health` reports the same
condition, so it cannot be GREEN while semantic recall is intentionally off.

## Failure boundary

An incompatible or corrupt Cursor database makes the overall index command
non-zero and prints the exact source error, but it does not roll back already
committed Claude/Codex work.  Unknown bubble fields are preserved generically
rather than silently disappearing. If a catalog row, conversation header, or
bubble body is no longer decodable, the Cursor pass fails closed before
reconciliation and preserves the last good snapshot for the next retry.

## Rejected alternatives

- **Keep virtual paths and query live SQLite during expansion.** Navigation
  disappears when Cursor is closed, upgraded, or the session is deleted; it
  also couples every MCP read to a private live database.
- **Store only surface chunks.** Exact grep and the reasoning/tool context that
  makes session recall useful remain impossible.
- **Copy db + WAL files.** A checkpoint between copies can produce a mismatched
  snapshot. SQLite's online backup API provides one coherent view.
- **Abort all indexing on a Cursor schema change.** Other sources are
  independent and their successful transactions remain valuable.

## Verification

Synthetic databases cover the observed Cursor 3.14.7 tables, catalog-less
legacy discovery, subagent filtering, numeric and ISO timestamps, the installed
client's nested `ConversationMessage.ToolResult` envelope, durable
expand/step/raw grep, session deletion, embedding-model swaps,
schema-drift preservation, and migration from virtual v1 paths with vector
reuse. A read-only live smoke test against Cursor 3.14.7 additionally covers
the SQLite backup, current bubble envelope, semantic anchor, expansion,
stepping, raw grep, and unchanged-session incremental path without sending
conversation text to a hosted embedder.
