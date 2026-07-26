# session-recall

Local, agentic **semantic recall over your Claude Code and Codex session history**. Both
hosts feed one index, so either agent can recover context created by the other. It gives the
agent five tools (via MCP) so it can resume past work instead of making you re-explain it:

- `recall_search(query)` — find a past discussion **by meaning** (not substring). Answers
  `{"anchors": [...], "degraded": null | str}`; `degraded` is set when the embedding provider was
  unreachable and only literal matching ran, so the agent can say so instead of mistaking a
  lexical miss for an empty history.
- `expand_around(session_id, uuid)` — a cursor into the raw turn (tool calls, outputs, thinking).
- `step(session_id, uuid, direction)` — move to an adjacent turn (cheap cursor step).
- `grep(pattern)` — on-demand substring scan over **all** indexed transcripts, including
  under-the-hood turns (tool output, thinking) that never became search chunks.
- `recent_sessions()` — the freshest past sessions first (what's current, how fresh the index is).

On-demand (no proactive auto-injection in v1). Local, open source.

`recall_search`, `grep` and `recent_sessions` also take an optional `scope_cwd` — pass your
current working directory to scope results to the current repo (worktrees collapse to the repo
root); omit it for cross-project recall. Ranked hits carry a human-readable `when_human`
timestamp alongside the raw epoch. Every MCP tool accepts an optional `source` (`claude` or
`codex`); omit it to use the unified history. Results include provenance as `source=claude` or
`source=codex`. The three discovery tools also accept `on_date` for one day or inclusive
`start_date` / `end_date` (`YYYY-MM-DD`) plus an optional IANA `timezone`, so an agent can
constrain retrieval to an actual local calendar day instead of hoping a date written into the
semantic query affects ranking. If `timezone` is omitted, Session Recall uses the timezone of
the computer running the MCP server.

**Status:** v1, built and validated on real history. Key design rationale lives in
[docs/decisions/](docs/decisions/).

## How it works

Claude Code transcripts and Codex sessions from `~/.codex/sessions` plus
`~/.codex/archived_sessions` share the same index.
Only the conversation "surface" is embedded — user prompts and assistant text replies.
Tool calls, results, reasoning, and other trace data are not embedded but stay reachable on
demand via `expand_around` (and `step`) or `grep`. Raw Codex transcript files remain local;
only the extracted conversation surface is sent to the configured embedding provider.

Embeddings: Voyage `voyage-4-large` (dim 1024) → SQLite
(`sqlite-vec` KNN + FTS5, bm25-ranked) → Voyage `rerank-2.5` → top-k. Indexing is
incremental (by file metadata, including Codex inode+size) and cheap on live transcripts: they are append-only, so
unchanged chunks are matched by content hash and their vectors reused — only new turns
hit the embedding API. Moving a Codex rollout into the archive also reuses its existing
vectors. Each file indexes in its own transaction; a failing file is
logged and retried on the next run, never aborting the rest. Claude sidechains
(`<session>/subagents/`) and Codex spawned-subagent sessions are intentionally skipped —
they are under-the-hood tooling, not the primary user/agent conversation.

Embeddings are pluggable (Voyage is the default); the reranker is optional, and the
system degrades gracefully to KNN + FTS without one. Switching the embedding
provider/model is detected (an embed fingerprint is part of each file's index
signature) and triggers a clean re-embed instead of silently mixing vector spaces.

## Install / run

For normal use with the bundled Claude Code/Codex plugins, install the two CLI entrypoints on
your user `PATH` (the manifests also check `~/.local/bin`):

```bash
pipx install --editable .
export VOYAGE_API_KEY=...                        # your Voyage key

session-recall index                             # both sources (same as --source all)
session-recall index --source claude             # only ~/.claude/projects
session-recall index --source codex              # active + archived Codex sessions
session-recall search "query"                    # unified semantic search
session-recall search "query" --source codex
session-recall recent --source claude            # freshest Claude sessions
session-recall recent --date 2026-07-14           # uses this computer's timezone
session-recall search "deployment work" --start-date 2026-07-14 \
  --end-date 2026-07-14 --timezone Asia/Yekaterinburg
session-recall grep "exact" --source codex --limit 100  # raw scan, no API needed
session-recall prune                             # drop rows for deleted transcripts
```

For repository development or manual MCP registration, an in-tree virtualenv works too:

```bash
python -m venv .venv && .venv/bin/pip install -e .
.venv/bin/session-recall index
```

`index --source` accepts `all`, `claude`, or `codex` and defaults to `all`. `search`,
`recent`, `grep`, and `prune` accept optional `--source claude|codex`; omit it for both.
`search`, `recent`, and `grep` accept `--date` for one day or `--start-date`, `--end-date`, and
`--timezone` for a range; range dates are inclusive and either boundary may be omitted. The
timezone defaults to the computer's local zone and can be overridden with an IANA name. Raw
`grep` returns at most 100 matches by default; use `--limit` to adjust the cap.

### Connect to Claude Code or Codex (MCP)

The repo includes both Claude Code and Codex plugin manifests around the same MCP server and
recall skill. To register the server manually in Claude Code:

```bash
claude mcp add session-recall --scope user -- \
  /absolute/path/.venv/bin/python -m session_recall.server
```

The server reads `VOYAGE_API_KEY` from the environment. For Codex, the
`.codex-plugin/plugin.json` manifest is ready to place in a local repo or personal marketplace;
follow the [local plugin installation guide](https://learn.chatgpt.com/docs/build-plugins#install-a-local-plugin-manually).
In Claude Code, verify with `claude mcp list` → `✔ Connected`. Start a new session/task after
enabling the plugin so the MCP tools and skill are loaded.

## Keeping the index fresh

Indexing is incremental (it skips already-indexed files by signature), so staying fresh is
cheap. The plugin's bundled `SessionStart` hook is compatible with both hosts and runs
`session-recall index` in the background; the default `--source all` refreshes both histories.
Codex requires a one-time review/trust of newly installed or changed hooks through `/hooks`.
For a manual Claude Code hook in `~/.claude/settings.json`:

```json
"hooks": {
  "SessionStart": [
    { "hooks": [ {
      "type": "command",
      "command": "sr=/abs/path/.venv/bin/session-recall; pgrep -f \"$sr index\" >/dev/null 2>&1 || (VOYAGE_API_KEY=... \"$sr\" index >/tmp/sr-index.log 2>&1 &)"
    } ] }
  ]
}
```

The `pgrep` guard prevents overlapping runs; `( … & )` detaches so session start doesn't
wait. Keep the host-level hook synchronous: the shell already backgrounds the indexer, and
Codex ignores Claude's `async` extension. A `launchd`/cron timer is another option. (Local on one machine is enough; a server-side
index only makes sense across several machines — at the cost of privacy and network.)

## Privacy — hard invariant

This is a public repository. **Only code goes in it.**

- Data, indexes, raw transcripts, embeddings → `~/.local/share/session-recall/`,
  **outside the repo tree**. They physically cannot be committed.
- API keys → environment only (`VOYAGE_API_KEY`); `.gitignore` blocks `.env`.
- Tests → synthetic fixtures only, never a real slice of a session.
- Claude Code transcripts plus active and archived Codex transcripts are read locally. Only
  user/assistant surface text is embedded; tool/reasoning trace data stays out of embeddings and
  is exposed only by explicit raw expansion or grep.
- Chunk texts ARE sent to your configured embedding/rerank provider (Voyage by
  default) — pick a provider you trust with your transcripts, or point the
  OpenAI-compatible provider at a local endpoint.
