---
id: inference-api-provider
type: service
title: Qwen inference gateway provider
summary: Embeddings and reranking through an authenticated capability-based HTTP API.
status: confirmed
tags: [qwen, inference, embeddings, reranker, codex]
canonical_for: [inference-api-provider]
verified_at: 2026-09-14
sources:
  - type: repository
    reference: src/session_recall/inference.py
    confirmed_at: 2026-09-14
  - type: repository
    reference: src/session_recall/config.py
    confirmed_at: 2026-09-14
  - type: repository
    reference: tests/test_inference.py
    confirmed_at: 2026-09-14
related: [../project.md, ../processes/inference-api-setup.md]
---

# Inference API provider

The `inference-api` preset selects the public `embedder` and `reranker` model
aliases. It defaults to 4096 embedding dimensions; deployments can override
that value. The base URL and credential have no default.

The client sends `POST embeddings` beneath the configured `/v1` base URL,
with `input_type=document` for indexing and `input_type=query` for retrieval.
It sends batches of up to 64 strings and requests base64 float32 vectors.
Response indices determine ordering. Both float arrays and base64 responses
are validated for dimension and finite values before storage.

Reranking uses `POST rerank` with `query`, `documents`, and `top_n`. Results
must contain distinct valid document indices and finite `relevance_score`
values. The client returns scores in descending order.

Both endpoints receive `truncate_prompt_tokens`, defaulting to 8192. This
limits model input; the full extracted text remains in the local index.
The gateway must support these request fields, including the query/document
instruction distinction; a generic embeddings-only API is not sufficient.

The client uses `INFERENCE_API_KEY`, verifies HTTPS for remote endpoints,
does not follow redirects, and ignores proxy environment variables. Local
loopback HTTP is supported. Requests use a 120-second timeout with a
10-second connection timeout and up to three attempts for transport errors,
429, and selected temporary server errors. HTTP error messages omit response
bodies so echoed transcripts do not enter indexing logs.

Index fingerprints include the provider, model alias, dimension, endpoint,
operator-supplied revision, token limit, and query/document preprocessing
version. Changing any of these requires compatible reindexing. Encoding and
batch size do not change the embedding space.

See the [setup procedure](../processes/inference-api-setup.md).
