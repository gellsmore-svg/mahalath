"""Adapter factory: name + config -> Adapter instance."""

from __future__ import annotations

from mahalath.adapters.base import Adapter, AdapterError
from mahalath.adapters.mock import MockAdapter
from mahalath.adapters.ollama_cli import OllamaCliAdapter
from mahalath.config import AppConfig


def make_adapter(name: str, config: AppConfig) -> Adapter:
    if name == "mock":
        return MockAdapter(default_model=config.runtime.model)
    if name == "ollama_cli":
        return OllamaCliAdapter(
            executable=config.runtime.ollama_executable,
            default_model=config.runtime.model,
            default_timeout_seconds=config.runtime.ollama_timeout_seconds,
        )
    if name == "claude_api":
        from mahalath.adapters.claude_api import ClaudeApiAdapter
        return ClaudeApiAdapter()
    raise AdapterError(
        f"Unknown adapter {name!r}. Supported: mock, ollama_cli, claude_api."
    )
