"""Detailed exposition of an accepted definition (same sense, more depth).

The debated ``DefinitionVersion.text`` stays the precise, consensus sense
used for identity, staleness, and short retrieval. ``detailed_text`` is an
optional longer elaboration of that *same* sense — pedagogical depth for
glossary readers and budgeted bundles — not a second frame or a competing
definition.

Generation is single-model and best-effort: accept paths never fail if
the expansion call errors or is disabled.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from pymongo.database import Database

from mahalath.adapters.base import Adapter, AdapterError
from mahalath.config import AppConfig
from mahalath.db.repositories import OntologyEntryRepository
from mahalath.style import load_style_overlay, render_style_block


log = logging.getLogger("mahalath.detailed")

# Soft target for model output length (characters). The prompt asks for
# several short paragraphs; we trim only pathological runaways.
_MAX_DETAILED_CHARS = 4000
_MIN_DETAILED_CHARS = 80


class DetailedError(Exception):
    """Raised when a detailed-description response cannot be used."""


@dataclass
class DetailedBackfillResult:
    entries_scanned: int = 0
    definitions_missing: int = 0
    written: int = 0
    skipped: int = 0
    errored: int = 0
    errors: list[str] = field(default_factory=list)
    details: list[dict[str, Any]] = field(default_factory=list)


def build_detailed_prompt(
    *,
    term: str,
    short_definition: str,
    context_name: str | None = None,
    source_snippet: str | None = None,
    style_overlay: str | None = None,
) -> str:
    """Prompt for a longer exposition of an already-accepted short definition."""
    parts: list[str] = [
        "You expand an already-accepted ontology definition into a richer "
        "description of the SAME sense. Do NOT invent a different meaning, "
        "do NOT add a second frame, and do NOT contradict the short definition.",
        "",
        f"Term: {term}",
    ]
    if context_name:
        parts.append(f"Frame (context): {context_name}")
    parts.append(f"Accepted short definition:\n{short_definition.strip()}")
    if source_snippet and source_snippet.strip():
        snippet = source_snippet.strip()
        if len(snippet) > 1500:
            snippet = snippet[:1500] + "…"
        parts.append("")
        parts.append(
            "Optional source context (use only to ground elaboration; "
            "do not quote at length):\n" + snippet
        )
    if style_overlay:
        parts.append("")
        parts.append(render_style_block(style_overlay))
    parts.append("")
    parts.append(
        "Write 2–4 short paragraphs (roughly 120–350 words) that:\n"
        "  - restate the sense in fuller prose for a careful reader\n"
        "  - note how the term is used in this corpus when relevant\n"
        "  - distinguish near-neighbours only if needed for clarity\n"
        "  - stay faithful to the short definition\n"
        "Output ONLY the exposition prose — no JSON, no heading, no bullet list."
    )
    return "\n".join(parts)


def parse_detailed_response(response_text: str) -> str:
    text = (response_text or "").strip()
    if not text:
        raise DetailedError("empty detailed-description response")
    # Strip accidental fences.
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 2:
            inner = parts[1]
            if inner.lstrip().startswith("json"):
                inner = inner.lstrip()[4:]
            text = inner.strip()
    # Drop a leading "Term:" label some models echo.
    text = re.sub(r"^(?:detailed\s+)?(?:description|exposition)\s*:\s*", "", text, flags=re.I)
    text = text.strip()
    if len(text) < _MIN_DETAILED_CHARS:
        raise DetailedError(
            f"detailed description too short ({len(text)} chars)"
        )
    if len(text) > _MAX_DETAILED_CHARS:
        text = text[:_MAX_DETAILED_CHARS].rsplit(" ", 1)[0] + "…"
    return text


def generate_detailed_text(
    *,
    term: str,
    short_definition: str,
    adapter: Adapter,
    style_overlay: str | None = None,
    context_name: str | None = None,
    source_snippet: str | None = None,
    model: str | None = None,
) -> str:
    """Ask ``adapter`` for a longer exposition; return the cleaned prose."""
    prompt = build_detailed_prompt(
        term=term,
        short_definition=short_definition,
        context_name=context_name,
        source_snippet=source_snippet,
        style_overlay=style_overlay,
    )
    response = adapter.generate(prompt, model=model, want_json=False)
    return parse_detailed_response(response.text)


def set_definition_detailed_text(
    db: Database,
    mpl_label: str,
    definition_index: int,
    detailed_text: str,
) -> bool:
    """Write ``detailed_text`` onto one definition slot. Returns False if missing."""
    entry = OntologyEntryRepository(db).get(mpl_label)
    if entry is None or not entry.definitions:
        return False
    if definition_index < 0 or definition_index >= len(entry.definitions):
        return False
    db.ontology_entries.update_one(
        {"_id": mpl_label},
        {
            "$set": {
                f"definitions.{definition_index}.detailed_text": detailed_text,
            }
        },
    )
    return True


def enrich_definition_with_detail(
    db: Database,
    mpl_label: str,
    *,
    adapter: Adapter,
    definition_index: int = -1,
    style_overlay: str | None = None,
    source_snippet: str | None = None,
    model: str | None = None,
    overwrite: bool = False,
) -> str | None:
    """Generate and store detailed_text for one definition. Best-effort.

    ``definition_index=-1`` means the latest definition. Returns the written
    text, or None when skipped / failed.
    """
    entry = OntologyEntryRepository(db).get(mpl_label)
    if entry is None or not entry.definitions:
        return None
    idx = definition_index if definition_index >= 0 else len(entry.definitions) - 1
    if idx < 0 or idx >= len(entry.definitions):
        return None
    definition = entry.definitions[idx]
    if definition.detailed_text and not overwrite:
        return definition.detailed_text

    context_name: str | None = None
    if definition.context_id:
        from mahalath.db.repositories import DefinitionContextRepository

        ctx = DefinitionContextRepository(db).get(definition.context_id)
        if ctx is not None:
            context_name = ctx.name

    try:
        detailed = generate_detailed_text(
            term=entry.canonical_term,
            short_definition=definition.text,
            adapter=adapter,
            style_overlay=style_overlay,
            context_name=context_name,
            source_snippet=source_snippet,
            model=model,
        )
    except (AdapterError, DetailedError) as exc:
        log.info(
            "detailed: skip %s[%d] — %s", mpl_label, idx, exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001 — never break accept path
        log.info("detailed: skip %s[%d] — unexpected %s", mpl_label, idx, exc)
        return None

    set_definition_detailed_text(db, mpl_label, idx, detailed)
    log.info("detailed: wrote %s[%d] (%d chars)", mpl_label, idx, len(detailed))
    return detailed


def backfill_detailed_definitions(
    config: AppConfig,
    db: Database,
    adapter: Adapter,
    *,
    max_items: int = 50,
    overwrite: bool = False,
    only_labels: set[str] | None = None,
    apply: bool = True,
) -> DetailedBackfillResult:
    """Walk definitions missing ``detailed_text`` and fill them (or dry-run)."""
    style_overlay = load_style_overlay(config)
    result = DetailedBackfillResult()
    query: dict[str, Any] = {}
    if only_labels is not None:
        query["_id"] = {"$in": sorted(only_labels)}

    for doc in db.ontology_entries.find(query):
        result.entries_scanned += 1
        label = doc.get("_id") or doc.get("mpl_label")
        definitions = doc.get("definitions") or []
        for idx, definition in enumerate(definitions):
            if not isinstance(definition, dict):
                continue
            text = (definition.get("text") or "").strip()
            if not text:
                continue
            existing = definition.get("detailed_text")
            if existing and not overwrite:
                result.skipped += 1
                continue
            result.definitions_missing += 1
            if not apply:
                result.details.append({
                    "mpl_label": label,
                    "definition_index": idx,
                    "status": "would-write",
                })
                continue
            try:
                written = enrich_definition_with_detail(
                    db,
                    str(label),
                    adapter=adapter,
                    definition_index=idx,
                    style_overlay=style_overlay,
                    overwrite=overwrite,
                )
            except Exception as exc:  # noqa: BLE001
                result.errored += 1
                result.errors.append(f"{label}[{idx}]: {exc}")
                continue
            if written:
                result.written += 1
                result.details.append({
                    "mpl_label": label,
                    "definition_index": idx,
                    "status": "written",
                    "chars": len(written),
                })
            else:
                result.errored += 1
                result.errors.append(f"{label}[{idx}]: generation failed")
            if result.written >= max_items:
                return result
    return result
