"""Subprocess adapter for the Windows-side Ollama binary.

Why subprocess and not the HTTP API: on WSL2 the Windows Ollama daemon
binds to Windows-side localhost; reaching it over HTTP from WSL needs
additional binding configuration on the Windows side that we don't
want to require of operators. The CLI route is already used by
Mnemosyne with the same Ollama install, so we follow that pattern for
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

import re
import subprocess
import time
from pathlib import Path

from mahalath.adapters.base import AdapterError, AdapterResponse

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
    ) -> None:
        if not Path(executable).exists():
            raise AdapterError(f"Ollama executable not found: {executable}")
        self.executable = Path(executable)
        self.default_model = default_model
        self.default_timeout_seconds = default_timeout_seconds

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
