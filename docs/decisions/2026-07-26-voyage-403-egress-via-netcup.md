# Route Voyage traffic through Netcup, and stop hiding FTS-only degradation

**Date:** 2026-07-26 · **Branch:** `claude/plugin-installation-17054b`

## Context

Recall had been quietly broken for a day and a half. `recent_sessions` reported
the freshest session as **2026-07-24 14:36**, while sessions kept being written
locally; the indexer log showed the same line on every run:

```
session-recall: 22 file(s) failed to index (will retry next run):
  <path>.jsonl: HTTP code 403 from API (<!doctype html>...403 Forbidden)
indexed 0 chunks from changed transcripts
```

Two separate failures stacked on top of each other:

- **Egress.** Voyage sits behind Google Cloud Armor and answers **403 with an HTML
  body** — not a JSON 401 — to requests from the home VPN exit
  (`91.232.114.134`, Telemagic B.V., NL, a datacenter ASN). The response is
  identical with no key, a garbage key, and a valid key, so the status code
  carries no diagnostic signal at all. `ai.mongodb.com` (the Atlas variant of the
  same API) is blocked from that IP too.
- **Silence.** `retrieve.py` caught the embedding failure and fell through to
  FTS-only with a bare `except: pass`. A lexical result set is shaped exactly like
  a semantic one, so the degradation was invisible: on 2026-07-25 a recall for
  "two services conflicting over an auth token" returned an unrelated status
  summary from 15 days earlier, and the conclusion drawn was "the plugin didn't
  work" rather than "the embedder is down".

## Decision

1. A permanent SOCKS5 exit through Netcup: `ssh -N -D 127.0.0.1:1080 netcup-socks`
   under a launchd agent (`RunAtLoad` + `KeepAlive`), with a dedicated ssh alias so
   it does not fight clawdbot over `LocalForward 18789`.
2. `VOYAGE_API_KEY` and `ALL_PROXY` live in Doppler (`session-recall/dev`). The
   existing `~/.local/bin/session-recall{,-mcp}` wrappers already ran under
   `doppler run` — they were repointed from the unrelated `sidekey` project to
   `session-recall`. Both Claude Code and Codex invoke those wrappers, so one
   change covers both hosts.
3. `PySocks` added to the venv — without it the SDK raises "Missing dependencies
   for SOCKS support" and the proxy silently does nothing.
4. `recall_search` now answers `{"anchors": [...], "degraded": null | str}`.
   Internally `SearchResult` subclasses `list`, so every existing caller keeps
   indexing and iterating unchanged.

## Why

The blockage is on the network path, not in the account or the key — so the fix
belongs on the network path too. Everything else (a proxy service, a migration of
the whole tool to the server) solves the same problem with far more moving parts.

The `degraded` flag matters independently of egress. The tool boundary is where an
agent decides whether to trust a miss. Without the flag, "embedder down" and "not
in the history" are indistinguishable, and the agent confidently reports the
second when the first is true — which is exactly what happened, twice, before this
was diagnosed.

## What we tested

| Probe | Result |
|---|---|
| `POST /v1/embeddings` from the Mac, no key | 403 HTML |
| same, garbage key | 403 HTML |
| same, valid key from Doppler | 403 HTML |
| `api.openai.com` without a key (control) | 401 JSON — so 403-HTML is a WAF, not an API |
| `POST /v1/embeddings` from Netcup, no key | **401** — the request reaches the API |
| via SOCKS tunnel, valid key | **200**, 1024-dim embedding |
| `ai.mongodb.com` from the Mac | 403 HTML — the Atlas endpoint is blocked as well |
| full re-index after the fix | **1323 chunks, zero failures** (was 0 chunks / 22 failures) |
| the 2026-07-25 query that missed | now ranks the right session first, score 0.863 |

DNS and the TLS chain were checked for interference (Google Trust Services, no
MITM by FortiClient) — the blocking is genuinely on Voyage's edge.

## Rejected

- **Take an Atlas key instead (`al-` prefix routes the SDK to `ai.mongodb.com`).**
  That host answers 403 from the same IP, so it changes nothing.
- **Reverse-proxy on Netcup (nginx + TLS + token).** Works from any machine and is
  the better answer once other people need it, but it publishes an endpoint to a
  paid API and adds a service to maintain — for a single user the tunnel is
  strictly less machinery.
- **Move session-recall onto the server entirely.** Considered first, since it
  also enables a team setup. It drags in transcript sync for both Claude and Codex,
  SSH-stdio for two clients, and moves the whole history off the laptop. Deferred
  until a team actually exists; the tunnel does not get in its way.
- **Copies of the key in `~/.claude/settings.json` and `~/.zshenv`.** Built, then
  reverted once it turned out the wrappers already inject from Doppler — three
  copies of a secret on disk to solve a problem that had none.
- **Adding PySocks to `pyproject.toml`.** The SOCKS hop is one user's network
  workaround, not a property of the tool.

---

Follow-up, deliberately left out of scope: `grep` is a literal substring scan but
silently accepts regex-looking patterns and returns zero hits for them — the same
class of defect as the silent FTS fallback, worth its own fix.
