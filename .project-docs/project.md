---
id: session-recall-project
type: project
title: Session Recall project overview
summary: Local-first semantic memory shared by Claude Code, Codex, and Cursor through one MCP interface.
status: confirmed
tags: [session-recall, semantic-search, mcp, claude-code, codex, cursor]
canonical_for: [project-overview, product-scope, architecture-entrypoints]
verified_at: 2026-08-03
sources:
  - type: repository
    reference: README.md
    confirmed_at: 2026-08-03
  - type: commit
    reference: d19054c4a83085ad7e02fb7221b068bc34299534
    confirmed_at: 2026-08-03
related: []
---

# Session Recall project overview

Session Recall gives Claude Code, Codex, and Cursor one searchable memory over
their local conversation histories. It indexes user and assistant conversation
surface text for semantic retrieval while preserving tool calls, outputs, and
reasoning for explicit raw navigation.

## Product boundaries

- The shipped runtime is a Python CLI and MCP server with native host plugin
  manifests for Claude Code, Codex, and Cursor.
- Recall is on demand. The project does not proactively inject history into
  every prompt.
- Original Claude Code and Codex transcripts stay in place. Cursor sessions are
  read from the editor's local SQLite store and normalized into durable local
  snapshots.
- A bundled local embedding model is the zero-key default. Hosted and
  user-operated embedding providers are optional.
- Team sharing is opt-in, end-to-end encrypted, project-scoped, secret-scanned,
  and approval-gated before an answer leaves its owner's machine.

## Architecture entrypoints

- `src/session_recall/index.py` and `src/session_recall/cursor.py`: source
  ingestion and incremental reconciliation.
- `src/session_recall/retrieve.py` and `src/session_recall/store.py`: hybrid
  semantic and lexical retrieval over SQLite.
- `src/session_recall/server.py`: the five MCP recall tools.
- `src/session_recall/cli.py`: setup, indexing, search, diagnostics, and local
  operations.
- `src/session_recall/share/`: optional cross-machine question and approval
  flow.
- `docs/decisions/`: detailed engineering rationale and invariants.
