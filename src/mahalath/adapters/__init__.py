"""Adapter package: model-runtime bridges.

Mahalath talks to language models through a narrow `Adapter` protocol so
the rest of the codebase doesn't depend on any specific runtime. Two
adapters are provided in Stage 1:

- `mock` for deterministic tests
- `ollama_cli` for the Windows-side Ollama binary, reached from WSL via
  subprocess (matches the Mnemosyne pattern: prompt over stdin, response
  over stdout, with a default 180s timeout).

Adapter selection at runtime is by `runtime.model_adapter` in the loaded
config; use `make_adapter(name, config)` to construct.
"""

from mahalath.adapters.base import Adapter, AdapterError, AdapterResponse
from mahalath.adapters.factory import make_adapter
from mahalath.adapters.mock import MockAdapter
from mahalath.adapters.ollama_cli import OllamaCliAdapter

__all__ = [
    "Adapter",
    "AdapterError",
    "AdapterResponse",
    "MockAdapter",
    "OllamaCliAdapter",
    "make_adapter",
]

# ClaudeApiAdapter is importable but not in __all__ because it has a hard
# httpx dependency at module-import time; callers that want it must
# `from mahalath.adapters.claude_api import ClaudeApiAdapter` explicitly.
