---
id: inference-api-setup
type: process
title: Connect a Qwen inference gateway
summary: Configure the gateway and build a separate index before switching retrieval.
status: confirmed
tags: [qwen, setup, migration, backup]
canonical_for: [inference-api-setup]
verified_at: 2026-09-14
sources:
  - type: repository
    reference: src/session_recall/config.py
    confirmed_at: 2026-09-14
  - type: repository
    reference: src/session_recall/inference.py
    confirmed_at: 2026-09-14
  - type: repository
    reference: src/session_recall/cli.py
    confirmed_at: 2026-09-14
related: [../project.md, ../services/inference-api.md]
---

# Connect a Qwen inference gateway

Check the [required gateway contract](../services/inference-api.md) first.
Obtain the endpoint, embedding dimension and context limit from the deployment
owner or authenticated model registry. Supply `INFERENCE_API_KEY` through your
secret manager or environment; never commit its value.

Example configuration for a gateway with 4096-dimensional vectors and an
8192-token context window:

```sh
export SESSION_RECALL_EMBED=inference-api
export SESSION_RECALL_EMBED_BASE_URL=https://inference.example/v1
export SESSION_RECALL_EMBED_DIM=4096
export SESSION_RECALL_INFERENCE_MAX_TOKENS=8192
export SESSION_RECALL_EMBED_REVISION=deployment-revision
export SESSION_RECALL_DB_PATH="$HOME/.local/share/session-recall/index-qwen.db"
session-recall index
session-recall health
session-recall search "why did we choose"
```

The preset uses model aliases `embedder` and `reranker`. Override them with
`SESSION_RECALL_EMBED_MODEL` and `SESSION_RECALL_RERANK_MODEL` if your gateway
uses different public names. Existing provider overrides also take precedence
over the preset, so remove or update stale overrides when migrating.

Before migration, preserve the old provider settings and make a consistent
SQLite backup of the old index. Build the new embedding space in a separate
file with `SESSION_RECALL_DB_PATH`; do not mix Voyage and Qwen vectors.
Review indexing errors, corpus coverage and health before switching clients.
An unavailable source database cannot supply new history; retain its old
index until its historical records have been accounted for.

Configure CLI invocations, background indexers, and MCP launchers with the same
provider settings and database path. Restart existing MCP processes after
switching, because they retain their configuration and open database.
To roll back, restore the previous launcher configuration and use the old
index with its original embedding provider.

When the backend behind the public alias changes incompatibly, update
`SESSION_RECALL_EMBED_REVISION` and rebuild. A stable model alias alone does
not identify an immutable embedding space. Session Recall does not auto-load
`.env` files; export these settings or load them through your launcher.
