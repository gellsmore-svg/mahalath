"""Deterministic mock adapter for tests.

Constructed with an optional mapping of prompt-substring -> response.
The first key found as a substring in the prompt wins; otherwise the
default response is returned. Useful for stubbing debate iterations
where each agent role expects a different response.

A `calls` list records every invocation so tests can assert on the
sequence and content of adapter calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mahalath.adapters.base import AdapterResponse


@dataclass
class MockAdapter:
    name: str = "mock"
    default_response: str = "ok"
    responses: dict[str, str] = field(default_factory=dict)
    default_model: str = "mock-model"
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
