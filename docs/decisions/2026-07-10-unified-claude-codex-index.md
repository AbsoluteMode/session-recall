# Unified Claude Code + Codex index

## Decision

Use one SQLite/vector index for both transcript producers, with explicit
`source = claude|codex` provenance on chunks and indexed files.

Codex discovery covers both active `CODEX_HOME/sessions/**/*.jsonl` rollouts
and `CODEX_HOME/archived_sessions/**/*.jsonl`. Spawned subagent rollouts are
skipped from the first `session_meta` marker (`thread_source`,
`source.subagent`, or `agent_path`) because they copy parent history and are
under-the-hood execution, matching the existing Claude sidechain policy.

## Extraction boundary

- Claude keeps its existing v2 extractor and signature, avoiding a costly
  re-embed of unchanged history.
- Codex has an independent `codex-v1` extractor signature.
- Codex `event_msg/user_message` and `event_msg/agent_message` records are the
  canonical embedded surface. `response_item` messages are deliberately kept
  out of embeddings because real rollouts can contain service instructions
  shaped like user text. They, tools, and reasoning remain available only
  through explicit navigation/grep.
- Codex records have no Claude-style UUID, so anchors use the stable opaque
  cursor `codex:<thread-id>:<byte-offset>`.

## Retrieval boundary

Production indexing and retrieval are streaming. `grep` scans line by line;
its result set is capped (100 by default), and
`expand_around` and `step` retain only a bounded deque around the requested
cursor. This is required because individual local Codex rollouts can be
hundreds of megabytes. Human-readable expansion and grep exclude encrypted
reasoning fields.

Codex archive moves reuse embeddings by `(source, session_id, content_hash)`.
Deleted-path pruning happens only after a successful replacement; a provider
failure therefore leaves the last good memory available for the next retry.

## Compatibility

Existing databases migrate in place: old chunks and indexed-file markers
default to `source = claude`. The original Python and MCP arguments remain
valid; `source` is additive and optional. Pre-fingerprint Claude signatures
are migrated in place to the current embedding-space signature, so installing
Codex support does not by itself re-embed the Claude corpus.
