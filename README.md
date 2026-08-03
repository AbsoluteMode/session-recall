# session-recall

*Русская версия: [docs/README.ru.md](docs/README.ru.md)*

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

## Where it pays off

Two months of daily use; the cases that stuck:

- **Session onboarding.** A fresh session starts already in context — whether you juggle
  several subscriptions, hop between coding agents (Claude reads what Codex worked out
  yesterday), brainstorm the same problem across parallel sessions, or return to a task
  you "discussed at some point".
- **Bugs and regressions.** Before fixing anything, the agent asks the history: *was this
  bug seen before? how was it fixed? why did we believe it was fixed?* A recurrence you
  yourself forgot stops looking like a fresh bug — and the fix turns from a patch into a
  dig into the component.
- **Actions.** Explain a procedure once — how to read a trace, fill your work
  spreadsheets, break down token spend per task per model — and any later session
  replays it without being walked through again.
- **Cause and effect.** Say "let's change this decision", and the agent looks up the
  moment it was made: *"we picked X for compatibility with Y — before changing anything,
  make sure Y survives."*

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

On-demand (no proactive auto-injection in v1). Local, open source. The tools are plain
MCP, so any MCP-capable agent — Cursor included — can search the same history; the
histories being indexed today come from Claude Code and Codex.

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

That is the whole thing: with no key and no local server, indexing runs on a
bundled CPU model, downloaded once and picked by your interaction language
(`SESSION_RECALL_LANG=en|zh|…`, multilingual when unset) — see
[Embedding providers](#embedding-providers) for the options. Hosted Voyage
embeddings rank noticeably better; to use them instead, export a key first:

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

Start here — it checks the whole chain and exits non-zero when something is actually
broken, so it also works from a timer:

```console
$ session-recall health
[ok  ] Freshness  2 minutes behind
[warn] Embedder   responded in 5828 ms
                  → slow provider will make indexing crawl
[ok  ] Corpus     1053 sessions (claude 373, codex 680)
[ok  ] Sources    claude, codex present

verdict: AMBER (voyage/voyage-4-large, index at ~/.local/share/session-recall/index.db)
```

Freshness compares the newest transcript on disk against the newest turn in the index,
so an indexer that runs on every session and fails every time still shows as behind —
which is exactly the failure that is otherwise invisible.

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
| `builtin-en` | **bundled, free** | `bge-small-en-v1.5` | 384 | — |
| `builtin-zh` | **bundled, free** | `bge-small-zh-v1.5` | 512 | — |
| `builtin-multi` | **bundled, free** | `paraphrase-multilingual-MiniLM-L12-v2` | 384 | — |

With no preset set, session-recall picks Voyage when `VOYAGE_API_KEY` is
present, then probes for a local server already listening, and otherwise runs
the **bundled ONNX model** — out of the box always works. The bundled flavor
follows the interaction language you choose at onboarding
(`SESSION_RECALL_LANG=en|zh|…`; a small English or Chinese specialist,
multilingual for every other answer and for no answer at all — `builtin` as a
preset name resolves the same way). First use downloads the model once into
the data dir (70–240MB), CPU inference from then on; ranking is noticeably
coarser than hosted Voyage — a starting point, not the ceiling. With a key
configured, no probe runs.

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

## Team mode — ask a colleague's history

The same recall, across machines: pair with a colleague once, and your agent can ask
their agent about their past work.

> **you → a colleague's agent:** when you hit the local-launch problem with X — how did
> you solve it?
>
> **their agent** *(after the colleague approves the answer)*: pin the config to …, then
> …, and the problem does not come back.

The agents negotiate the details between themselves; what used to be a Slack thread and a
half-remembered explanation becomes one question and one grounded answer. You never see
the colleague's raw history — only the approved answer.

Privacy here is mechanics, not policy:

- questions and answers travel as **end-to-end encrypted envelopes** through a relay that
  stores blind blobs — the relay cannot read them;
- answers are built by an **isolated read-only worker**, scoped to the projects that
  contact was explicitly granted (`share allow`);
- every candidate answer passes a **secret scanner** and then **explicit approval** by
  the owner (Telegram bot, or `share approve` locally) before it leaves the machine;
- a contact can be paused any time (`share pause`), a peer revoked (`share revoke`).

### Pick a transport (once, before pairing)

Nothing is baked in: a fresh install has **no transport** and never talks to a
server you didn't choose. The relay is blind — everything it carries is sealed
and signed on the clients — so which one to use is coordination between peers,
not a matter of trust. Three recipes, simplest first:

**Shared folder — zero infrastructure.** Two accounts on one machine, or any
folder both peers sync (Syncthing, Dropbox, an NFS mount):

```bash
export SESSION_RECALL_SHARE_TRANSPORT_DIR=~/Sync/sr-share   # both peers, same folder
```

**Your relay on the LAN.** One machine runs it, everyone points at it.
Envelopes are end-to-end encrypted regardless, but this is plain HTTP —
mailbox addresses travel readable, so keep it to a network you trust:

```bash
session-recall share relay --port 8787 --host 0.0.0.0       # on the relay machine
export SESSION_RECALL_RELAY_URL=http://192.168.1.20:8787    # on every peer
```

**Your relay on the internet.** The relay binds localhost on purpose and
expects a TLS terminator in front. On the server:

```bash
pipx install git+https://github.com/AbsoluteMode/session-recall
session-recall share relay --port 8787    # binds 127.0.0.1
```

As a user systemd service (`~/.config/systemd/user/sr-relay.service`):

```ini
[Unit]
Description=session-recall share relay

[Service]
ExecStart=%h/.local/bin/session-recall share relay --port 8787
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now sr-relay
loginctl enable-linger $USER              # keep it alive after logout
```

Any TLS proxy in front works; Caddy is the two-line option:

```
relay.example.com {
    reverse_proxy 127.0.0.1:8787
}
```

Then on every peer: `export SESSION_RECALL_RELAY_URL=https://relay.example.com`.
The relay stores only sealed blobs under `<data-dir>/share-relay`, and a
mailbox is emptied on fetch; `SESSION_RECALL_RELAY_URL=none` keeps an install
network-silent on purpose. Put the `export` in your shell profile so agents
and timers see it too.

### Pairing

Pairing is a one-time ceremony with a short SAS check, then asking is one command:

```bash
export SESSION_RECALL_RELAY_URL=https://relay.example.com   # both sides: the transport you picked
session-recall share init            # once per device, both sides
session-recall share invite          # you: prints a one-time code
session-recall share join <code>     # colleague: accepts it
session-recall share complete        # you: finish the handshake
session-recall share trust <name>    # both: confirm the SAS matched, name the peer
session-recall share allow <name> <project>
session-recall share notify          # owner side: worker + approval loop

session-recall share ask <name> "how did you fix the local X launch?"
session-recall share fetch           # collect the answers
```

Searching a peer's index needs no embedding setup on your side: the query travels as
text, and the owner's worker embeds it with their own provider against their own index.

## meta docs — the project's memory, written down

Raw recall answers "what was said". meta docs answers the questions agents
actually ask mid-task: *was this bug fixed before? how do I perform this
action? why was it decided this way?* A daily job hands each session's
dialogue — user messages and final answers only, never the tool noise — to a
distiller agent that maintains entries in a git repository of your choice:

- `<project>/bugs/` — bugs that were actually fixed: how each was recognized,
  diagnosed, fixed, and proven fixed;
- `<project>/actions/` — procedures, step by step, written so an agent asked
  again can follow the entry alone;
- `<project>/decisions/` — contested choices: what was decided, why that way,
  what was rejected;
- `USER/` — a global map of where your information lives and *how to find it*
  (lookup commands, storage locations — never the stored values themselves).

One file per entry, with related PRs and source sessions in the frontmatter.

```bash
session-recall metadocs init ~/meta-docs --from-today   # memory starts now
session-recall metadocs run                             # one pass now
session-recall metadocs enable                          # daily launchd job (default 21:00)
session-recall metadocs status
session-recall metadocs index-history --days 30         # opt-in: distill the past, once
```

The agent's whole world is four MCP verbs — `search / create / edit /
delete` — and the load-bearing rules are server mechanics, not prompt
requests: `create` is refused until the agent has `search`ed (dedup is
mandatory), entries are scanned for secrets before a byte reaches disk, and
`delete` demands a reason. Every built-in tool is stripped from the call.
Runs are incremental (per-session watermarks) and each changed project gets
its own commit — review is a diff, undo is a revert, and sharing the memory
with a team is just pushing the repo somewhere private. Commits stay local
unless you opt into `--push`; the distilling agent and model come from config only
(`init --engine claude-cli|codex --model … [--reasoning …]`) — nothing is picked
silently. The daily job never touches history; `index-history` is the only door to
the past.

## Roadmap

- **Hosted/team index** — one shared index for a team instead of per-machine copies. The
  honest open question: whoever searches must embed the query, so a shared vector space
  implies a shared embedding path (a team key, or a small embedding proxy in front of
  the provider).
- **Per-contact bypass** — skip per-answer approval for peers you fully trust; today
  every answer is approved explicitly.
- **More histories** — Cursor and other agents' transcripts as index sources.

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
