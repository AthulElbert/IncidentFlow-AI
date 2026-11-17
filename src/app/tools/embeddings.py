"""Turning text into vectors for incident memory.

`OpenAICompatibleEmbedder` calls any `/embeddings` endpoint (OpenAI, Gemini's OpenAI-compatible API, or a local
server), asking for a fixed number of dimensions so vectors fit pgvector's indexes (at most 2000). Transient errors
are retried, waiting as long as the provider asks.

`HashingEmbedder` needs no network and is deterministic, which makes it right for tests and the wrong choice for
anything else: it hashes words, so it captures shared words and no meaning at all.
"""
import hashlib
import json
import math
import re
import time
from typing import Callable, Protocol
from urllib import request
from urllib.error import HTTPError, URLError

from app.adapters.llm_client import TRANSIENT_STATUSES, LLMError, retry_after_seconds

MAX_BATCH = 64
MAX_RETRY_AFTER_SECONDS = 60.0


class Embedder(Protocol):
    model: str
    dimensions: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


class HashingEmbedder:
    """Feature hashing of words. Deterministic and offline; captures no meaning."""

    def __init__(self, dimensions: int = 256) -> None:
        self.dimensions = dimensions
        self.model = f"hashing-{dimensions}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for word in re.findall(r"[a-z0-9]+", text.lower()):
                digest = hashlib.sha256(word.encode()).digest()
                vector[int.from_bytes(digest[:4], "big") % self.dimensions] += 1.0 if digest[4] % 2 else -1.0
            norm = math.sqrt(sum(v * v for v in vector)) or 1.0
            vectors.append([v / norm for v in vector])
        return vectors


class OpenAICompatibleEmbedder:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        dimensions: int = 768,
        timeout_seconds: int = 30,
        max_retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not (base_url and api_key and model):
            raise ValueError("embeddings need a base URL, an API key and a model")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dimensions = dimensions
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._sleep = sleep

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), MAX_BATCH):
            vectors.extend(self._batch(texts[start : start + MAX_BATCH]))
        return vectors

    def _batch(self, texts: list[str]) -> list[list[float]]:
        for attempt in range(self.max_retries + 1):
            try:
                return self._request(texts)
            except LLMError as exc:
                if not exc.transient or attempt >= self.max_retries:
                    raise
                wait = exc.retry_after if exc.retry_after is not None else 0.5 * (2**attempt)
                if wait > MAX_RETRY_AFTER_SECONDS:
                    raise  # a daily quota, say: not worth waiting for
                self._sleep(wait)
        raise AssertionError("unreachable")

    def _request(self, texts: list[str]) -> list[list[float]]:
        body = json.dumps({"model": self.model, "input": texts, "dimensions": self.dimensions}).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/embeddings",
            data=body,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore") if exc.fp else ""
            transient = exc.code in TRANSIENT_STATUSES or exc.code >= 500
            wait = retry_after_seconds(exc.headers.get("Retry-After") if exc.headers else None, detail)
            raise LLMError(f"embeddings API error {exc.code}: {detail[:400]}", exc.code, transient, wait) from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise LLMError(f"embeddings connection error: {exc}", transient=True) from exc

        try:
            items = sorted(data["data"], key=lambda item: item.get("index", 0))
            vectors = [[float(v) for v in item["embedding"]] for item in items]
        except (KeyError, TypeError, ValueError) as exc:
            raise LLMError("embeddings reply did not have the expected shape") from exc
        if len(vectors) != len(texts) or any(len(v) != self.dimensions for v in vectors):
            raise LLMError(f"embeddings reply had {len(vectors)} vector(s) of the wrong size; expected {len(texts)} of {self.dimensions}")
        return vectors
