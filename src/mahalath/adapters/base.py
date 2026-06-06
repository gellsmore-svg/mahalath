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


@dataclass
class AdapterResponse:
    text: str
    model: str
    duration_ms: int
    raw: dict[str, Any] = field(default_factory=dict)


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
