# session-recall

Local, agentic **semantic recall over your Claude Code session history**. It gives the
agent five tools (via MCP) so it can resume past work instead of making you re-explain it:

- `recall_search(query)` — find a past discussion **by meaning** (not substring).
- `expand_around(session_id, uuid)` — a cursor into the raw turn (tool calls, outputs, thinking).
- `step(session_id, uuid, direction)` — move to an adjacent turn (cheap cursor step).
- `grep(pattern)` — on-demand substring scan over **all** indexed transcripts, including
  under-the-hood turns (tool output, thinking) that never became search chunks.
- `recent_sessions()` — the freshest past sessions first (what's current, how fresh the index is).

On-demand (no proactive auto-injection in v1). Local, open source.

`recall_search`, `grep` and `recent_sessions` also take an optional `scope_cwd` — pass your
current working directory to scope results to the current repo (worktrees collapse to the repo
root); omit it for cross-project recall. Ranked hits carry a human-readable `when_human`
timestamp alongside the raw epoch.

**Status:** v1, built and validated on real history. Key design rationale lives in
[docs/decisions/](docs/decisions/).

## How it works

Only the conversation "surface" is indexed — user prompts and assistant text replies.
`tool_result` / `thinking` / harness noise are not embedded but stay reachable via
`expand_around` / `grep`. Embeddings: Voyage `voyage-4-large` (dim 1024) → SQLite
(`sqlite-vec` KNN + FTS5, bm25-ranked) → Voyage `rerank-2.5` → top-k. Indexing is
incremental (by mtime+size) and cheap on live transcripts: they are append-only, so
unchanged chunks are matched by content hash and their vectors reused — only new turns
hit the embedding API. Each file indexes in its own transaction; a failing file is
logged and retried on the next run, never aborting the rest. Subagent sidechains
(`<session>/subagents/`) are intentionally skipped — that's under-the-hood tooling,
not conversation.

Embeddings are pluggable (Voyage is the default); the reranker is optional, and the
system degrades gracefully to KNN + FTS without one. Switching the embedding
provider/model is detected (an embed fingerprint is part of each file's index
signature) and triggers a clean re-embed instead of silently mixing vector spaces.

## Install / run

```bash
python -m venv .venv && .venv/bin/pip install -e .
export VOYAGE_API_KEY=...                        # your Voyage key

.venv/bin/session-recall index                   # index ~/.claude/projects
.venv/bin/session-recall search "query"          # semantic search from the CLI
.venv/bin/session-recall recent                  # freshest sessions (is the index current?)
.venv/bin/session-recall grep "exact string"     # raw substring scan, no API needed
.venv/bin/session-recall prune                   # drop rows for deleted transcripts
```

### Connect to Claude Code (MCP)

```bash
claude mcp add session-recall --scope user -- \
  /absolute/path/.venv/bin/python -m session_recall.server
```

The server reads `VOYAGE_API_KEY` from the environment; the tools (`recall_search` and
friends) become available to the agent in new sessions. Verify with
`claude mcp list` → `✔ Connected`.

## Keeping the index fresh

Indexing is incremental (it skips already-indexed files by signature), so staying fresh is
cheap. The most direct way is a Claude Code `SessionStart` hook that runs
`session-recall index` in the background at the start of each session. In
`~/.claude/settings.json`:

```json
"hooks": {
  "SessionStart": [
    { "hooks": [ {
      "type": "command",
      "async": true,
      "command": "pgrep -f 'session-recall index' >/dev/null 2>&1 || (VOYAGE_API_KEY=... /abs/path/.venv/bin/session-recall index >/tmp/sr-index.log 2>&1 &)"
    } ] }
  ]
}
```

The `pgrep` guard prevents overlapping runs; `( … & )` detaches so session start doesn't
wait. A `launchd`/cron timer works too. (Local on one machine is enough; a server-side
index only makes sense across several machines — at the cost of privacy and network.)

## Privacy — hard invariant

This is a public repository. **Only code goes in it.**

- Data, indexes, raw transcripts, embeddings → `~/.local/share/session-recall/`,
  **outside the repo tree**. They physically cannot be committed.
- API keys → environment only (`VOYAGE_API_KEY`); `.gitignore` blocks `.env`.
- Tests → synthetic fixtures only, never a real slice of a session.
- Chunk texts ARE sent to your configured embedding/rerank provider (Voyage by
  default) — pick a provider you trust with your transcripts, or point the
  OpenAI-compatible provider at a local endpoint.
