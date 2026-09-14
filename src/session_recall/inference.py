"""Clients for a Qwen inference gateway with public capability model aliases."""
import base64
import math
import os
import struct
import time
from urllib.parse import urlparse

import httpx

from . import config


class InferenceClient:
    def __init__(self, base_url=None, api_key=None):
        self.base_url = (base_url or config.EMBED_BASE_URL or "").rstrip("/")
        self.api_key = api_key
        self._client = None

    @property
    def client(self):
        if self._client is None:
            url = urlparse(self.base_url)
            if (url.scheme not in ("http", "https") or not url.hostname
                    or url.username or url.password or url.query or url.fragment):
                raise ValueError("Set SESSION_RECALL_EMBED_BASE_URL to the gateway /v1 URL")
            if url.scheme != "https" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
                raise ValueError("Remote inference gateways require HTTPS")
            key = self.api_key or os.environ.get("INFERENCE_API_KEY")
            if not key:
                raise ValueError("INFERENCE_API_KEY is required")
            self._client = httpx.Client(
                base_url=self.base_url + "/",
                headers={"Authorization": "Bearer " + key},
                timeout=httpx.Timeout(120, connect=10),
                # The Voyage SOCKS egress must not intercept corporate traffic.
                trust_env=False, follow_redirects=False,
            )
        return self._client

    def post(self, endpoint, payload):
        for attempt in range(3):
            try:
                response = self.client.post(endpoint, json=payload)
            except httpx.TransportError:
                if attempt == 2:
                    raise RuntimeError("Inference API transport failed after 3 attempts") from None
            else:
                if response.is_success:
                    return response.json()
                if response.status_code not in (429, 500, 502, 503, 504) or attempt == 2:
                    # Never put provider bodies (which can echo text) in indexing logs.
                    raise RuntimeError(f"Inference API {endpoint}: HTTP {response.status_code}")
            time.sleep(2 ** attempt)


class InferenceEmbedder:
    def __init__(self, model=None, dim=None, client=None):
        self.model = model or config.EMBED_MODEL
        self.dim = dim or config.EMBED_DIM
        self.client = client or InferenceClient()

    def _embed(self, texts, input_type):
        out = []
        # Keep background batches bounded on the shared GPU.
        for start in range(0, len(texts), 64):
            batch = texts[start:start + 64]
            data = self.client.post("embeddings", {
                "model": self.model, "input": batch, "input_type": input_type,
                "encoding_format": "base64",
                "truncate_prompt_tokens": int(os.environ.get("SESSION_RECALL_INFERENCE_MAX_TOKENS", "8192")),
            })["data"]
            if (len(data) != len(batch)
                    or sorted(item["index"] for item in data) != list(range(len(batch)))):
                raise ValueError("Inference API returned invalid embedding indices")
            for item in sorted(data, key=lambda item: item["index"]):
                vector = item["embedding"]
                if isinstance(vector, str):
                    raw = base64.b64decode(vector, validate=True)
                    if len(raw) != self.dim * 4:
                        raise ValueError("Inference API changed embedding dimensions")
                    vector = list(struct.unpack(f"<{self.dim}f", raw))
                if (len(vector) != self.dim
                        or any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in vector)):
                    raise ValueError("Inference API returned an invalid embedding or changed dimensions")
                out.append(vector)
        return out

    def embed_documents(self, texts):
        return self._embed(texts, "document")

    def embed_query(self, text):
        return self._embed([text], "query")[0]


class InferenceReranker:
    def __init__(self, model=None, client=None):
        self.model = model or config.RERANK_MODEL
        self.client = client or InferenceClient()

    def rerank(self, query, documents, top_k):
        if not documents or top_k <= 0:
            return []
        results = self.client.post("rerank", {
            "model": self.model, "query": query,
            "documents": documents, "top_n": min(top_k, len(documents)),
            "truncate_prompt_tokens": int(os.environ.get("SESSION_RECALL_INFERENCE_MAX_TOKENS", "8192")),
        })["results"]
        ranked = []
        seen = set()
        for item in results:
            index, score = item["index"], item["relevance_score"]
            if (type(index) is not int or not 0 <= index < len(documents)
                    or index in seen or not isinstance(score, (int, float))
                    or not math.isfinite(score)):
                raise ValueError("Inference API returned invalid reranking results")
            seen.add(index)
            ranked.append((index, float(score)))
        if len(ranked) != min(top_k, len(documents)):
            raise ValueError("Inference API returned an incomplete reranking result")
        return sorted(ranked, key=lambda item: item[1], reverse=True)[:top_k]
