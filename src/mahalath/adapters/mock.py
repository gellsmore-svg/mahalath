"""Deterministic mock adapter for tests.

Constructed with an optional mapping of prompt-substring -> response.
The first key found as a substring in the prompt wins; otherwise the
default response is returned. Useful for stubbing debate iterations
where each agent role expects a different response.

A `calls` list records every invocation so tests can assert on the
sequence and content of adapter calls.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any

from mahalath.adapters.base import AdapterResponse, EmbeddingResponse


@dataclass
class MockAdapter:
    name: str = "mock"
    default_response: str = "ok"
    responses: dict[str, str] = field(default_factory=dict)
    default_model: str = "mock-model"
    # text-substring -> vector, same first-match-wins idiom as `responses`.
    # Unmatched text gets a deterministic hash-derived unit vector, so the
    # mock is stable across runs without a real embedding model.
    embeddings: dict[str, list[float]] = field(default_factory=dict)
    embedding_dim: int = 8
    calls: list[dict[str, Any]] = field(default_factory=list)

    def generate(
        self,
        prompt: str,
        *,
        model: str | None = None,
        timeout_seconds: int | None = None,
        want_json: bool = False,
    ) -> AdapterResponse:
        self.calls.append(
            {
                "prompt": prompt,
                "model": model,
                "timeout_seconds": timeout_seconds,
                "want_json": want_json,
            }
        )
        text = self.default_response
        for needle, response in self.responses.items():
            if needle in prompt:
                text = response
                break
        return AdapterResponse(
            text=text,
            model=model or self.default_model,
            duration_ms=0,
        )

    def embed(
        self,
        text: str,
        *,
        model: str | None = None,
        timeout_seconds: int | None = None,
    ) -> EmbeddingResponse:
        self.calls.append({"embed": text, "model": model})
        for needle, vector in self.embeddings.items():
            if needle in text:
                return EmbeddingResponse(
                    vector=list(vector), model=model or self.default_model,
                    dim=len(vector),
                )
        vector = self._hash_vector(text)
        return EmbeddingResponse(
            vector=vector, model=model or self.default_model, dim=len(vector),
        )

    def _hash_vector(self, text: str) -> list[float]:
        """A stable pseudo-embedding: hash bytes → floats → unit length.
        Deterministic per text; not semantic, but enough for wiring tests."""
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        raw = [digest[i % len(digest)] - 127.5 for i in range(self.embedding_dim)]
        norm = math.sqrt(sum(x * x for x in raw)) or 1.0
        return [x / norm for x in raw]
