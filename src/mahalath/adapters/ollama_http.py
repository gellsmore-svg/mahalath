"""HTTP adapter for Ollama (`model_adapter = "ollama_http"`).

Talks to Ollama purely over its HTTP API — no `ollama` binary on PATH required.
This is the portable counterpart to `ollama_cli`: use it whenever Ollama is reached
over HTTP (the common case on a native install, or WSL with `OLLAMA_HOST=0.0.0.0`
on the Windows side / the `wsl-gateway` sentinel). Generation hits `/api/generate`;
embeddings hit `/api/embed` (same contract `ollama_cli.embed` already uses).
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request

from mahalath.adapters.base import (
    AdapterError,
    AdapterResponse,
    EmbeddingNaNError,
    EmbeddingResponse,
)


def _post_json(url: str, payload: dict, *, timeout: int) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class OllamaHttpAdapter:
    """Adapter that calls Ollama over HTTP (no subprocess)."""

    name: str = "ollama_http"

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11434",
        default_model: str = "gemma4:e2b",
        default_timeout_seconds: int = 180,
        embedding_model: str = "bge-m3",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model
        self.default_timeout_seconds = default_timeout_seconds
        self.embedding_model = embedding_model

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
        payload: dict = {"model": model_name, "prompt": prompt, "stream": False}
        if want_json:
            payload["format"] = "json"
        start = time.monotonic()
        try:
            body = _post_json(f"{self.base_url}/api/generate", payload, timeout=timeout)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:300]
            except Exception:  # pragma: no cover
                pass
            raise AdapterError(
                f"Ollama generate HTTP {exc.code} at {self.base_url}/api/generate: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise AdapterError(
                f"Ollama generate could not connect to {self.base_url}/api/generate "
                f"({exc.reason}). Is the daemon reachable from here?"
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise AdapterError(f"Ollama generate failed for {model_name}: {exc}") from exc

        text = str(body.get("response", "")).strip()
        return AdapterResponse(
            text=text,
            model=model_name,
            duration_ms=int((time.monotonic() - start) * 1000),
            raw={k: body.get(k) for k in ("total_duration", "eval_count") if k in body},
        )

    def embed(
        self,
        text: str,
        *,
        model: str | None = None,
        timeout_seconds: int | None = None,
    ) -> EmbeddingResponse:
        model_name = model or self.embedding_model
        timeout = timeout_seconds or self.default_timeout_seconds
        start = time.monotonic()
        try:
            body = _post_json(
                f"{self.base_url}/api/embed", {"model": model_name, "input": text},
                timeout=timeout,
            )
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:300]
            except Exception:  # pragma: no cover
                pass
            if "NaN" in detail or "Inf" in detail:
                raise EmbeddingNaNError(
                    f"{model_name} produced a non-finite embedding for this input: {detail[:160]}"
                ) from exc
            raise AdapterError(
                f"Ollama embed HTTP {exc.code} at {self.base_url}/api/embed: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise AdapterError(
                f"Ollama embed could not connect to {self.base_url}/api/embed ({exc.reason})."
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise AdapterError(f"Ollama embed failed for {model_name}: {exc}") from exc

        vectors = body.get("embeddings")
        vector = vectors[0] if vectors else body.get("embedding")
        if not isinstance(vector, list) or not vector:
            raise AdapterError(
                f"Ollama embed returned no vector for {model_name}: {str(body)[:200]}"
            )
        floats = [float(x) for x in vector]
        if any(not math.isfinite(x) for x in floats):
            raise EmbeddingNaNError(f"{model_name} returned a non-finite embedding value.")
        return EmbeddingResponse(
            vector=floats,
            model=model_name,
            dim=len(floats),
            duration_ms=int((time.monotonic() - start) * 1000),
        )
