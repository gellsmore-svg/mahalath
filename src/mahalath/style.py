"""Per-corpus style-prompt overlay loader and renderer.

Agents working on a specialised corpus benefit from author-voice notes
and domain-vocabulary overrides that aren't in the model's training.
Without them, gemma4:e2b confidently writes "Ontology is the
philosophical study of being" — the standard dictionary entry — even
when the corpus opens with "Ontology means: what is something,
really?".

This module loads an optional Markdown overlay file (path configured
via `runtime.style_overlay_path`) and renders it into a stable wrapper
that prompt builders inject between their role preamble and their
task instructions.

The overlay is free-form Markdown. Typical content:

  - Definitional priorities ("definitions are covenant-neutral but
    scripture-authoritative").
  - Voice conventions ("declarative ontology, no interpretive
    hedging").
  - Domain vocabulary ("RS = Relational Substrate; vortons are stable
    topological knots").
  - Term overrides ("'Ontology' in this corpus means 'what something
    really is', not the academic discipline").

Loading is idempotent and cheap; the CLI typically loads once at the
start of process-document and passes the rendered text through every
adapter call.
"""

from __future__ import annotations

from pathlib import Path

from mahalath.config import AppConfig


STYLE_OVERLAY_HEADER = "## Style guidance for this corpus"
STYLE_OVERLAY_FOOTER = "(end of style guidance — task instructions follow)"


def load_style_overlay(
    config: AppConfig, *, project_root: Path | None = None
) -> str | None:
    """Load the configured style overlay file if any, else return None."""
    overlay_path = config.runtime.style_overlay_path
    if not overlay_path:
        return None
    root = project_root or Path.cwd()
    path = Path(overlay_path)
    if not path.is_absolute():
        path = root / path
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def render_style_block(overlay: str | None) -> str:
    """Wrap overlay text in stable header/footer so the model sees a clear
    boundary between corpus guidance and the task that follows.

    Returns an empty string when `overlay` is None or whitespace-only —
    callers can splice this in unconditionally and Stage 1 prompts are
    byte-identical when no overlay is configured.
    """
    if not overlay or not overlay.strip():
        return ""
    return (
        f"{STYLE_OVERLAY_HEADER}\n"
        "\n"
        f"{overlay.strip()}\n"
        "\n"
        f"{STYLE_OVERLAY_FOOTER}\n"
    )
