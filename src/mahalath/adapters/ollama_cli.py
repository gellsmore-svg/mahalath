"""Subprocess adapter for the Windows-side Ollama binary.

Why subprocess and not the HTTP API: on WSL2 the Windows Ollama daemon
binds to Windows-side localhost; reaching it over HTTP from WSL needs
additional binding configuration on the Windows side that we don't
want to require of operators. The CLI route is already used by
Tirzah with the same Ollama install, so we follow that pattern for
operational parity.

Invocation:

    <executable> run <model> --nowordwrap [--think=false --hidethinking]
                            [--format json]
    stdin:  <prompt>
    stdout: <response text>

The two flags `--think=false` and `--hidethinking` may not exist on
older Ollama builds; if Ollama reports an unknown flag, we retry once
without them.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from mahalath.adapters.base import (
    AdapterError,
    AdapterResponse,
    EmbeddingNaNError,
    EmbeddingResponse,
)

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _clean_terminal_output(raw: str) -> str:
    """Strip ANSI escapes and CR artifacts emitted by the Windows TTY."""
    text = _ANSI_ESCAPE.sub("", raw)
    text = text.replace("\r", "")
    return text.strip()


class OllamaCliAdapter:
    """Adapter that shells out to the Windows Ollama binary."""

    name: str = "ollama_cli"

    def __init__(
        self,
        executable: Path,
        *,
        default_model: str = "gemma4:e2b",
        default_timeout_seconds: int = 180,
        base_url: str = "http://localhost:11434",
        embedding_model: str = "bge-m3",
    ) -> None:
        if not Path(executable).exists():
            raise AdapterError(f"Ollama executable not found: {executable}")
        self.executable = Path(executable)
        self.default_model = default_model
        self.default_timeout_seconds = default_timeout_seconds
        self.base_url = base_url.rstrip("/")
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

        attempts: list[list[str]] = [
            self._build_command(model_name, want_json=want_json, modern=True),
            self._build_command(model_name, want_json=want_json, modern=False),
        ]

        last_err: str | None = None
        for cmd in attempts:
            start = time.monotonic()
            try:
                result = subprocess.run(
                    cmd,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    encoding="utf-8",
                    errors="replace",
                )
            except subprocess.TimeoutExpired as exc:
                raise AdapterError(
                    f"Ollama timed out after {timeout}s running {model_name}"
                ) from exc
            except FileNotFoundError as exc:
                raise AdapterError(
                    f"Ollama executable disappeared at {self.executable}"
                ) from exc

            if result.returncode == 0:
                duration_ms = int((time.monotonic() - start) * 1000)
                text = _clean_terminal_output(result.stdout)
                return AdapterResponse(
                    text=text,
                    model=model_name,
                    duration_ms=duration_ms,
                    raw={"command": cmd, "stderr": result.stderr.strip()[:500]},
                )

            stderr_lower = result.stderr.lower()
            if "unknown flag" in stderr_lower or "unrecognized" in stderr_lower:
                last_err = result.stderr
                continue
            raise AdapterError(
                f"Ollama exited {result.returncode} on {model_name}: "
                f"{result.stderr.strip()[:500]}"
            )

        raise AdapterError(
            f"All Ollama invocation attempts failed for {model_name}; "
            f"last stderr: {last_err}"
        )

    def _build_command(
        self, model: str, *, want_json: bool, modern: bool
    ) -> list[str]:
        cmd = [str(self.executable), "run", model, "--nowordwrap"]
        if modern:
            cmd.extend(["--think=false", "--hidethinking"])
        if want_json:
            cmd.extend(["--format", "json"])
        return cmd

    def embed(
        self,
        text: str,
        *,
        model: str | None = None,
        timeout_seconds: int | None = None,
    ) -> EmbeddingResponse:
        """Embed via Ollama's HTTP `/api/embed` endpoint.

        Generation shells out to the CLI (the Windows daemon is awkward
        to reach over HTTP from WSL), but the CLI has no stable
        embedding output contract across versions, whereas `/api/embed`
        does: POST {"model", "input"} -> {"embeddings": [[...]]}. If the
        daemon isn't reachable over HTTP from here, this raises a clear
        AdapterError and the caller can fall back to prompt-based
        candidate selection.
        """
        model_name = model or self.embedding_model
        timeout = timeout_seconds or self.default_timeout_seconds
        payload = json.dumps({"model": model_name, "input": text}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/api/embed",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        start = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # HTTPError is reachable-but-errored (subclass of URLError, so
            # caught first). bge-m3 emits NaN on some inputs, which Ollama
            # then can't serialise → 500 "unsupported value: NaN".
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:300]
            except Exception:  # pragma: no cover
                pass
            if "NaN" in detail or "Inf" in detail:
                raise EmbeddingNaNError(
                    f"{model_name} produced a non-finite embedding for this "
                    f"input (model numerical instability): {detail[:160]}"
                ) from exc
            raise AdapterError(
                f"Ollama embed HTTP {exc.code} at {self.base_url}/api/embed: "
                f"{detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise AdapterError(
                f"Ollama embed could not connect to {self.base_url}/api/embed "
                f"({exc.reason}). Is OLLAMA_HOST=0.0.0.0 set on the daemon and "
                f"the host reachable from here?"
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise AdapterError(
                f"Ollama embed failed for {model_name}: {exc}"
            ) from exc

        # /api/embed returns {"embeddings": [[...]]}; the older
        # /api/embeddings returned {"embedding": [...]}. Accept both.
        vectors = body.get("embeddings")
        vector = vectors[0] if vectors else body.get("embedding")
        if not isinstance(vector, list) or not vector:
            raise AdapterError(
                f"Ollama embed returned no vector for {model_name}: "
                f"{str(body)[:200]}"
            )
        floats = [float(x) for x in vector]
        if any(not math.isfinite(x) for x in floats):  # NaN/Inf via a 200
            raise EmbeddingNaNError(
                f"{model_name} returned a non-finite embedding value."
            )
        return EmbeddingResponse(
            vector=floats,
            model=model_name,
            dim=len(floats),
            duration_ms=int((time.monotonic() - start) * 1000),
        )
