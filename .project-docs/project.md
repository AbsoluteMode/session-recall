---
id: session-recall-project
type: project
title: Session Recall
summary: Semantic retrieval over local Claude Code, Codex, and Cursor history.
status: confirmed
tags: [recall, indexing, embeddings]
canonical_for: [project-overview]
verified_at: 2026-09-14
sources:
  - type: repository
    reference: README.md
    confirmed_at: 2026-09-14
related: [services/inference-api.md, processes/inference-api-setup.md]
---

# Session Recall

Session Recall extracts conversation text, stores text and vectors in SQLite,
and exposes retrieval through its CLI and MCP server. Provider selection and
index identity are configured outside the repository.

See the [Inference API contract](services/inference-api.md) and
[connection procedure](processes/inference-api-setup.md) for hosted Qwen gateways.
The root README covers existing providers and the remaining product workflows.
