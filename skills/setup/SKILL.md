---
name: setup
description: Set up or reconfigure session-recall for the user — install, pick the interaction language, choose a local or cloud embedder, optionally enable team sharing. Trigger when the user asks to "set up session-recall", "настрой session-recall", install it, change its embedder/language, or enable sharing with a colleague. The user answers three questions in chat; you run the commands.
---

# Agent-driven setup

You are the installer. Ask the user the three questions below IN CHAT, in the
user's language, then run the commands yourself. Never invent flags — every
command you need is written here. All state lives outside the repo
(`~/.local/share/session-recall/`), so nothing here touches their project.

## 0. Install (skip if `session-recall --help` already works)

```bash
pipx install git+https://github.com/AbsoluteMode/session-recall
```

Plugin (gives the agent recall tools + auto-fresh index): in Claude Code run
`/plugin marketplace add AbsoluteMode/session-recall` then
`/plugin install session-recall`; in Cursor add this repository as a plugin
marketplace and run `/add-plugin session-recall`; other MCP hosts register
`session-recall-mcp` directly.

## 1. Three questions (ask in chat, one message)

1. **Language** you mostly work in with your agent (en / zh / ru / other) —
   picks the bundled embedding model; specialists exist for en and zh,
   everything else gets the multilingual model.
2. **Embedder: local or cloud?**
   - *local, zero setup* — bundled CPU model, free, nothing leaves the machine,
     coarser ranking. Also fine: an already-running ollama/lmstudio is picked
     up automatically.
   - *cloud (Voyage)* — noticeably better ranking + reranker; transcript
     surface text is sent to the provider; needs `VOYAGE_API_KEY`.
3. **Solo or team?** Solo is the default (nothing is shared, nothing listens).
   Team = pair with specific colleagues; every answer to them passes a secret
   scanner and the user's explicit approval.

## 2. Apply

Language (always):

```bash
session-recall setup --lang <en|zh|ru|…> --yes
```

`--yes` also runs the first index. Warn first when the history is large
(`setup` prints the size): on the bundled CPU model months of history can
take a while; that is normal, later runs are incremental.

Cloud embedder: the key must NOT pass through you when avoidable — ask the
user to add `export VOYAGE_API_KEY=…` (from voyageai.com) to their shell
profile themselves, then re-run `session-recall index` in a fresh shell.
Never write an API key into any file inside a repository.

Local server instead of the bundled model (optional):
`export SESSION_RECALL_EMBED=ollama` (needs `ollama pull nomic-embed-text`).

## 3. Team (only if chosen)

Transport first — both peers, same choice (README → Team mode has the
self-host recipes):

```bash
export SESSION_RECALL_RELAY_URL=https://relay.example.com   # their relay
# or a folder both peers sync:
export SESSION_RECALL_SHARE_TRANSPORT_DIR=~/Sync/sr-share
```

Then the ceremony — the invite code and the SAS check travel between the
HUMANS out-of-band (that is the security model; do not relay them yourself):

```bash
session-recall share init <their-name>
session-recall share invite            # user A: hand the code to the colleague
session-recall share join <code>       # user B
session-recall share complete          # user A
session-recall share trust <petname>   # BOTH, after the SAS codes match aloud
session-recall share allow <project> --to <petname>   # default is deny-all
session-recall share pause             # any time: stop answering
```

## 4. Verify — always finish with this

```bash
session-recall health
```

GREEN/AMBER verdict → done; show the user one real search over their own
history (`session-recall search "…"` with something they actually worked on)
so the first impression is a hit, not a config file. RED → the verdict names
the broken dimension and the fix; resolve it before declaring success.
