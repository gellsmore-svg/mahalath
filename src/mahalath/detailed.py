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
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pymongo.database import Database

from mahalath.adapters.base import Adapter, AdapterError
from mahalath.config import AppConfig
from mahalath.db.models import (
    ELABORATED,
    AgentExchange,
    DebateMessage,
    DecisionLogEntry,
)
from mahalath.db.repositories import (
    AgentExchangeRepository,
    DecisionLogRepository,
    OntologyEntryRepository,
)
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
    # Generation calls made this run. `max_items` bounds THIS, not `written`,
    # so a failing model cannot walk the whole collection.
    attempted: int = 0
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


@dataclass(frozen=True)
class DetailedGeneration:
    """One expansion call, with everything needed to audit it (ADR-034)."""

    text: str
    prompt: str
    raw_response: str
    model: str


def generate_detailed(
    *,
    term: str,
    short_definition: str,
    adapter: Adapter,
    style_overlay: str | None = None,
    context_name: str | None = None,
    source_snippet: str | None = None,
    model: str | None = None,
) -> DetailedGeneration:
    """Ask ``adapter`` for a longer exposition, keeping the full exchange.

    The prompt and raw response are returned alongside the cleaned prose so
    the caller can write the conversation record ADR-034 requires — without
    them the expansion is unauditable after the fact.
    """
    prompt = build_detailed_prompt(
        term=term,
        short_definition=short_definition,
        context_name=context_name,
        source_snippet=source_snippet,
        style_overlay=style_overlay,
    )
    response = adapter.generate(prompt, model=model, want_json=False)
    used = model or getattr(response, "model", None) or getattr(
        adapter, "default_model", None
    ) or "unknown"
    return DetailedGeneration(
        text=parse_detailed_response(response.text),
        prompt=prompt,
        raw_response=response.text,
        model=str(used),
    )


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
    return generate_detailed(
        term=term,
        short_definition=short_definition,
        adapter=adapter,
        style_overlay=style_overlay,
        context_name=context_name,
        source_snippet=source_snippet,
        model=model,
    ).text


def record_detailed_audit(
    db: Database,
    *,
    term: str,
    mpl_label: str,
    source_document_id: str,
    generation: DetailedGeneration,
) -> str:
    """Write decision_log + agent_exchanges for one expansion call.

    Returns the new ``decision_log_id``. Recorded in the same collection as
    debates so one lookup serves every prose layer, but with the non-debate
    outcome ``elaborated`` so effectiveness statistics stay about debates
    (ADR-034).
    """
    decision_log_id = str(uuid4())
    DecisionLogRepository(db).insert(
        DecisionLogEntry(
            decision_log_id=decision_log_id,
            term=term,
            source_document_id=source_document_id,
            messages=[
                DebateMessage(
                    iteration=1,
                    role="detailed_exposition",
                    content=generation.text,
                    model=generation.model,
                )
            ],
            iterations_used=1,
            outcome=ELABORATED,
            resulting_mpl_labels=[mpl_label],
        )
    )
    AgentExchangeRepository(db).insert(
        AgentExchange(
            decision_log_id=decision_log_id,
            iteration=1,
            role="detailed_exposition",
            model=generation.model,
            prompt=generation.prompt,
            response=generation.raw_response,
        )
    )
    return decision_log_id


def source_snippet_for_entry(
    db: Database,
    entry: Any,
    *,
    source_document_id: str | None = None,
    window_chars: int = 1500,
) -> str | None:
    """Best-effort passage from the entry's source document mentioning the term.

    The exposition prompt asks the model to note how a term is used *in this
    corpus*. Without a snippet the model has no corpus to consult and invents
    one, so every path that generates prose must supply this (ADR-035).

    Prefers ``source_document_id`` (the document that triggered the write)
    over the entry's first source, which is wrong for a multi-source entry.
    Returns None when the archived text is unavailable — callers degrade to a
    snippet-free prompt rather than failing.
    """
    doc_ids = list(getattr(entry, "source_document_ids", None) or [])
    doc_id = source_document_id or (doc_ids[0] if doc_ids else None)
    if not doc_id:
        return None
    record = db.documents.find_one(
        {"document_id": doc_id}, {"archive_path": 1, "source_path": 1}
    )
    if not record:
        return None
    from pathlib import Path

    for key in ("archive_path", "source_path"):
        raw = record.get(key)
        if not raw:
            continue
        path = Path(str(raw))
        try:
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        term = str(getattr(entry, "canonical_term", "") or "").strip()
        pos = text.lower().find(term.lower()) if term else -1
        if pos < 0:
            return text[:window_chars].strip() or None
        half = window_chars // 2
        start = max(0, pos - half)
        return text[start : start + window_chars].strip() or None
    return None


def set_definition_detailed_text(
    db: Database,
    mpl_label: str,
    definition_index: int,
    detailed_text: str,
    *,
    model_used: str | None = None,
    decision_log_id: str | None = None,
    created_at: datetime | None = None,
) -> bool:
    """Write ``detailed_text`` and its provenance onto one definition slot.

    The prose and the record of who wrote it land in a single update, so a
    stored exposition is never left without provenance (ADR-034/ADR-035).
    Returns False if the slot is missing.
    """
    entry = OntologyEntryRepository(db).get(mpl_label)
    if entry is None or not entry.definitions:
        return False
    if definition_index < 0 or definition_index >= len(entry.definitions):
        return False
    prefix = f"definitions.{definition_index}"
    db.ontology_entries.update_one(
        {"_id": mpl_label},
        {
            "$set": {
                f"{prefix}.detailed_text": detailed_text,
                f"{prefix}.detailed_model_used": model_used,
                f"{prefix}.detailed_decision_log_id": decision_log_id,
                f"{prefix}.detailed_created_at": created_at
                or datetime.now(timezone.utc),
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
    source_document_id: str | None = None,
) -> str | None:
    """Generate and store detailed_text for one definition. Best-effort.

    ``definition_index=-1`` means the latest definition. Returns the written
    text, or None when skipped / failed.

    ``source_document_id`` is the document that TRIGGERED this write. Pass it
    explicitly wherever it is known: falling back to the entry's first source
    is wrong for a multi-source entry, and the audit record needs the document
    actually being processed (ADR-033 prerequisite).

    Raises :class:`DetailedError` when generation fails, so callers can report
    the real reason rather than a fixed string; the accept/redefine hooks
    swallow it to stay best-effort.
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
        generation = generate_detailed(
            term=entry.canonical_term,
            short_definition=definition.text,
            adapter=adapter,
            style_overlay=style_overlay,
            context_name=context_name,
            source_snippet=source_snippet,
            model=model,
        )
    except (AdapterError, DetailedError) as exc:
        log.info("detailed: skip %s[%d] — %s", mpl_label, idx, exc)
        raise DetailedError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — never break accept path
        log.info("detailed: skip %s[%d] — unexpected %s", mpl_label, idx, exc)
        raise DetailedError(f"unexpected {type(exc).__name__}: {exc}") from exc

    # Audit first, then store: a definition must never carry prose whose
    # conversation was not recorded (ADR-034).
    doc_id = source_document_id or (
        entry.source_document_ids[0] if entry.source_document_ids else ""
    )
    decision_log_id: str | None = None
    try:
        decision_log_id = record_detailed_audit(
            db,
            term=entry.canonical_term,
            mpl_label=mpl_label,
            source_document_id=doc_id,
            generation=generation,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "detailed: audit write failed for %s[%d] — %s; not storing prose",
            mpl_label, idx, exc,
        )
        raise DetailedError(f"audit write failed: {exc}") from exc

    set_definition_detailed_text(
        db,
        mpl_label,
        idx,
        generation.text,
        model_used=generation.model,
        decision_log_id=decision_log_id,
    )
    log.info(
        "detailed: wrote %s[%d] (%d chars, model=%s, log=%s)",
        mpl_label, idx, len(generation.text), generation.model, decision_log_id,
    )
    return generation.text


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
    """Walk definitions missing ``detailed_text`` and fill them (or dry-run).

    ``max_items`` bounds ATTEMPTS, not successes — a run against an
    unreachable model must stop after the number asked for rather than
    walking the whole collection. This matches ``backfill-intents`` and
    ``backfill-contexts``, which slice their work list up front.

    A development convenience only: per ADR-035 no production write path may
    leave a field for this to fill.
    """
    style_overlay = load_style_overlay(config)
    result = DetailedBackfillResult()
    query: dict[str, Any] = {}
    if only_labels is not None:
        query["_id"] = {"$in": sorted(only_labels)}

    for doc in db.ontology_entries.find(query):
        result.entries_scanned += 1
        label = doc.get("_id") or doc.get("mpl_label")
        source_document_id = (doc.get("source_document_ids") or [None])[0]
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
                if result.definitions_missing >= max_items:
                    return result
                continue
            result.attempted += 1
            try:
                written = enrich_definition_with_detail(
                    db,
                    str(label),
                    adapter=adapter,
                    definition_index=idx,
                    style_overlay=style_overlay,
                    overwrite=overwrite,
                    source_document_id=source_document_id,
                )
            except Exception as exc:  # noqa: BLE001
                # Keep the adapter's own message: it usually names the host,
                # the errno and the remedy, and "generation failed" does not.
                result.errored += 1
                result.errors.append(f"{label}[{idx}]: {exc}")
                written = None
            if written:
                result.written += 1
                result.details.append({
                    "mpl_label": label,
                    "definition_index": idx,
                    "status": "written",
                    "chars": len(written),
                })
            if result.attempted >= max_items:
                return result
    return result
