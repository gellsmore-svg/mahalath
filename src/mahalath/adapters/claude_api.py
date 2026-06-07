"""HTTP adapter for Anthropic's Claude API.

The default-target use case is the `frontier_review` pass: a more
capable model drains pending_review queue items so the human only
sees genuinely-frontier cases. The same adapter is usable for any
generation task the local model would otherwise handle.

Authentication: `ANTHROPIC_API_KEY` environment variable (or pass
`api_key=` explicitly when constructing). No key persistence in the
codebase.

Defaults to Opus 4.7 because frontier-review benefits from the most
capable model, and the operational volume (a queue of dozens of items
per run, ~5K tokens in / ~200 tokens out) keeps cost modest. Override
with `--model claude-sonnet-4-6` for higher-volume runs.
"""

from __future__ import annotations

import os
import time

import httpx

from mahalath.adapters.base import AdapterError, AdapterResponse

DEFAULT_CLAUDE_MODEL = "claude-opus-4-7"
DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_MAX_TOKENS = 4096
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


class ClaudeApiAdapter:
    name: str = "claude_api"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        default_model: str = DEFAULT_CLAUDE_MODEL,
        default_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        default_max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise AdapterError(
                "ClaudeApiAdapter requires ANTHROPIC_API_KEY environment "
                "variable or explicit api_key= argument."
            )
        self.default_model = default_model
        self.default_timeout_seconds = default_timeout_seconds
        self.default_max_tokens = default_max_tokens

    def generate(
        self,
        prompt: str,
        *,
        model: str | None = None,
        timeout_seconds: int | None = None,
        want_json: bool = False,
    ) -> AdapterResponse:
        model_name = model or self.default_model
        timeout = timeout_seconds or self.default_timeout_seconds

        body: dict = {
            "model": model_name,
            "max_tokens": self.default_max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if want_json:
            body["system"] = (
                "You output ONLY a single valid JSON object that exactly "
                "matches the schema described in the user's prompt. No "
                "preamble, no markdown fences, no commentary outside the "
                "JSON."
            )

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }

        start = time.monotonic()
        try:
            response = httpx.post(API_URL, json=body, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            raise AdapterError(f"Claude API request failed: {exc}") from exc

        if response.status_code != 200:
            raise AdapterError(
                f"Claude API returned HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )

        data = response.json()
        content = data.get("content", [])
        text_blocks = [
            block.get("text", "")
            for block in content
            if block.get("type") == "text"
        ]
        text = "\n".join(text_blocks).strip()
        duration_ms = int((time.monotonic() - start) * 1000)
        return AdapterResponse(
            text=text,
            model=data.get("model", model_name),
            duration_ms=duration_ms,
            raw={
                "usage": data.get("usage", {}),
                "stop_reason": data.get("stop_reason"),
            },
        )
