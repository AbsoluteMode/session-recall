# session-recall

**Shared memory for Claude Code and Codex.** Pick up work from a month ago without
re-explaining it — and Claude can read what Codex worked out yesterday, because both engines
feed one index. Not a summary file someone maintains by hand: the actual turns, including tool
calls and reasoning, searchable by meaning.

```console
$ session-recall index
indexed 2175 chunks from changed transcripts

your history: 1052 sessions spanning 168 days, 40,035 searchable fragments
  Claude Code 372 · Codex 680
  busiest: sidekey, trend_detection, glitch
```

Then your agent stops asking you what you were doing:

> **you:** we were fixing the auth token conflict between the two services — where did we land?
>
> **agent:** *(recall_search → expand_around)* Both services shared one OAuth account, and the
> provider rotates refresh tokens per account, so each refresh invalidated the other's copy. You
> rejected the shared-credentials-directory patch as too coupled, and settled on a keeper service
> owning the session. The spec was never written — that was the next step.

Five tools over MCP:

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

## Install

Two pieces: a Python CLI (which also ships the MCP server) and a plugin that wires it into your
agent. Budget about two minutes plus the first index run.

### 1. CLI

```bash
pipx install git+https://github.com/AbsoluteMode/session-recall
session-recall index   # first run walks your whole history; later runs are incremental
```

That is the whole thing if you have a local embedding server running — see
[Embedding providers](#embedding-providers) for the free local setup. For hosted Voyage
embeddings instead, export a key first:

```bash
export VOYAGE_API_KEY=...   # voyageai.com; put the line in your shell profile
```

`pipx` puts `session-recall` and `session-recall-mcp` on `~/.local/bin` — exactly where the
plugin manifests look for them. The first index depends on how much history you have: minutes
for months of transcripts, seconds after that.

### 2. Plugin

**Claude Code**

```
/plugin marketplace add AbsoluteMode/session-recall
/plugin install session-recall
```

Then start a new session — MCP servers and skills load at session start, not on install.

**Codex** — the `.codex-plugin/plugin.json` manifest is ready to drop into a local repo or your
personal marketplace; see the
[local plugin installation guide](https://learn.chatgpt.com/docs/build-plugins#install-a-local-plugin-manually).
Codex also asks you to review newly installed hooks once via `/hooks`.

### 3. Check it works

```bash
session-recall search "something you actually discussed last week"
```

Hits with a `score` mean semantic search is live. In the agent, `claude mcp list` should show
`session-recall ✔ Connected`, and asking it about past work should trigger `recall_search`.

Nothing else to configure: the bundled `SessionStart` hook re-indexes in the background from
then on, so the index keeps up with both hosts on its own.

### Troubleshooting

| Symptom | Cause |
|---|---|
| `recall_search` answers with `degraded` set | The embedding provider is unreachable — only literal word matching ran. Results are still real, but a miss proves nothing. |
| Indexer logs `HTTP code 403` with an HTML body | Not your key: a WAF is blocking your IP (common on VPN and datacenter exits). Same 403 appears with no key at all. Route egress elsewhere or switch provider. |
| `Missing dependencies for SOCKS support` | A SOCKS proxy is set in the environment but `PySocks` is not installed in that venv. |
| `recent_sessions` shows an old timestamp | The indexer has not succeeded recently. Run `session-recall index` by hand and read the output. |

### CLI reference

```bash
session-recall index --source claude|codex|all   # defaults to all
session-recall search "query" --source codex
session-recall recent --date 2026-07-14          # this computer's timezone
session-recall search "deployment work" --start-date 2026-07-14 \
  --end-date 2026-07-14 --timezone Asia/Yekaterinburg
session-recall grep "exact" --limit 100          # raw scan, no API key needed
session-recall prune                             # drop rows for deleted transcripts
```

`search`, `recent`, `grep`, and `prune` all take an optional `--source claude|codex`; omit it to
search both. Date filters are inclusive and either boundary may be omitted; the timezone
defaults to this computer's and accepts any IANA name. `grep` caps at 100 matches by default.

For development, an in-tree virtualenv works too:

```bash
python -m venv .venv && .venv/bin/pip install -e .
.venv/bin/session-recall index
```

To register the MCP server by hand instead of using the plugin:

```bash
claude mcp add session-recall --scope user -- /absolute/path/.venv/bin/session-recall-mcp
```

## Embedding providers

Nothing is locked to one vendor. `SESSION_RECALL_EMBED=<preset>` sets endpoint, model,
dimension and reranker together, because those four are not independent choices:

| preset | runs | model | dim | reranker |
|---|---|---|---|---|
| `voyage` | hosted, needs a key | `voyage-4-large` | 1024 | `rerank-2.5` |
| `ollama` | **local, free** | `nomic-embed-text` | 768 | — |
| `lmstudio` | **local, free** | `nomic-embed-text-v1.5` | 768 | — |
| `openai` | hosted, needs a key | `text-embedding-3-large` | 1024 | — |

With no preset set, session-recall picks Voyage when `VOYAGE_API_KEY` is present, and
otherwise probes for a local server already listening — better than defaulting to a
provider that is guaranteed to reject the request. With a key configured, no probe runs.

**Free and local, start to finish:**

```bash
ollama pull nomic-embed-text
export SESSION_RECALL_EMBED=ollama
session-recall index
```

**Your own endpoint** — any server speaking `/v1/embeddings` (llama.cpp, vLLM, a
company gateway). Individual variables always beat the preset, so mix freely:

```bash
export SESSION_RECALL_EMBED_PROVIDER=openai-compatible
export SESSION_RECALL_EMBED_BASE_URL=https://embeddings.internal/v1
export SESSION_RECALL_EMBED_MODEL=your-model
export SESSION_RECALL_EMBED_DIM=1024
```

Two things worth knowing before you switch:

- **A different embedder needs its own index.** Vector tables are fixed-width, so
  changing the model or dimension means rebuilding: delete
  `~/.local/share/session-recall/index.db` and re-run `index`. Attempting to reuse the old
  one now fails with a message saying exactly that, rather than looking like a dead
  embedder.
- **Local presets ship no reranker**, so ranking is KNN + FTS only — good enough, but
  noticeably coarser than the hosted path.

On model choice: `nomic-embed-text` is the default because it is Apache-2.0 and installs in
one command. Stronger small models exist — `jina-embeddings-v5-text-nano` scores far higher
for its size — but they are **CC BY-NC**, which anyone indexing work history would be
violating without ever being told. If your use is genuinely non-commercial, point the
variables above at one. If you work in more than English, `qwen3-embedding:0.6b` (Apache-2.0)
handles multilingual history far better than `nomic`.

## Keeping the index fresh

If you installed the plugin, this is already handled — skip to the next section. The bundled
`SessionStart` hook works on both hosts and runs `session-recall index` in the background, and
the default `--source all` refreshes both histories. Indexing is incremental (it skips
already-indexed files by signature), so staying fresh is cheap.

Only if you registered the MCP server by hand, add the hook yourself in
`~/.claude/settings.json`:

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
