"""Adapter factory: name + config -> Adapter instance."""

from __future__ import annotations

from mahalath.adapters.base import Adapter, AdapterError
from mahalath.adapters.mock import MockAdapter
from mahalath.adapters.ollama_cli import OllamaCliAdapter
from mahalath.config import AppConfig

# Sentinel hostname in ollama_base_url meaning "resolve the Windows host
# at runtime" — the WSL2 default gateway. Embeddings reach the Windows
# Ollama daemon over HTTP, and that gateway IP changes across WSL
# restarts, so a hardcoded IP rots; this keeps it self-healing.
WSL_GATEWAY_SENTINEL = "wsl-gateway"


def _wsl_gateway_ip() -> str | None:
    """The Windows host's IP as seen from WSL2: the default-route gateway,
    with the resolv.conf nameserver as a fallback. None if undetectable."""
    import subprocess

    try:
        out = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=5,
        )
        parts = out.stdout.split()
        if "via" in parts:
            return parts[parts.index("via") + 1]
    except Exception:
        pass
    try:
        with open("/etc/resolv.conf", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("nameserver"):
                    return line.split()[1]
    except Exception:
        pass
    return None


def resolve_base_url(base_url: str) -> str:
    """Substitute the WSL-gateway sentinel host with the live gateway IP;
    pass any other URL through unchanged."""
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(base_url)
    if parsed.hostname != WSL_GATEWAY_SENTINEL:
        return base_url
    ip = _wsl_gateway_ip()
    if not ip:
        return base_url  # couldn't resolve; embed() will report clearly
    netloc = f"{ip}:{parsed.port}" if parsed.port else ip
    return urlunparse(parsed._replace(netloc=netloc))


def make_adapter(name: str, config: AppConfig) -> Adapter:
    if name == "mock":
        return MockAdapter(default_model=config.runtime.model)
    if name == "ollama_cli":
        return OllamaCliAdapter(
            executable=config.runtime.ollama_executable,
            default_model=config.runtime.model,
            default_timeout_seconds=config.runtime.ollama_timeout_seconds,
            base_url=resolve_base_url(config.runtime.ollama_base_url),
            embedding_model=config.runtime.embedding_model,
        )
    if name == "claude_api":
        from mahalath.adapters.claude_api import ClaudeApiAdapter
        return ClaudeApiAdapter()
    if name == "hoglah":
        from mahalath.adapters.hoglah import HoglahAdapter

        h = config.runtime.hoglah
        return HoglahAdapter(
            db_path=h.db_path,
            output_dir=h.output_dir,
            default_model=config.runtime.model,
            embedding_model=config.runtime.embedding_model,
            default_timeout_seconds=config.runtime.ollama_timeout_seconds,
            poll_interval_seconds=h.poll_interval_seconds,
            delivery=h.delivery,
            callback_host=h.callback_host,
            callback_port=h.callback_port,
        )
    raise AdapterError(
        f"Unknown adapter {name!r}. Supported: mock, ollama_cli, claude_api, hoglah."
    )
