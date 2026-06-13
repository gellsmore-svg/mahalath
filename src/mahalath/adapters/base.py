"""Adapter protocol and shared response/error types.

The protocol is deliberately narrow: a single `generate` method taking a
prompt and returning the response text plus minimal metadata (model
name used, wall-clock duration). Streaming, multi-turn chat, and tool
calling are out of scope for Stage 1; they can be added as new methods
on the protocol later without breaking existing call sites.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class AdapterError(Exception):
    """Raised when an adapter fails to produce a response.

    The debate loop catches this and routes the term to undecided (with
    reason='conflict' or 'iteration_cap' depending on context) rather
    than crashing the whole batch.
    """


class EmbeddingNaNError(AdapterError):
    """The embedding model returned (or failed to encode) a non-finite
    value — a numerical-instability quirk some models (e.g. bge-m3) hit
    on particular inputs. Distinct from a connection failure so callers
    can retry with different input or skip the entry, not abort."""


@dataclass
class AdapterResponse:
    text: str
    model: str
    duration_ms: int
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class EmbeddingResponse:
    """A single text's embedding: a fixed-length list of numbers whose
    closeness to another text's list reflects how related the meanings
    are. `dim` is len(vector), recorded so vectors from different
    models/sizes are never compared by accident."""

    vector: list[float]
    model: str
    dim: int
    duration_ms: int = 0


@runtime_checkable
class Adapter(Protocol):
    """Generation adapter.

    Implementations must set a `name` attribute matching the
    `runtime.model_adapter` key in config (`mock`, `ollama_cli`, ...).
    """

    name: str

    def generate(
        self,
        prompt: str,
        *,
        model: str | None = None,
        timeout_seconds: int | None = None,
        want_json: bool = False,
    ) -> AdapterResponse: ...

    def embed(
        self,
        text: str,
        *,
        model: str | None = None,
        timeout_seconds: int | None = None,
    ) -> EmbeddingResponse:
        """Return the embedding for `text`. Adapters without an embedding
        model raise AdapterError."""
        ...
