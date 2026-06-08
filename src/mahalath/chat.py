"""Chat backend: grounded answers to natural-language questions about the ontology.

First slice is intentionally narrow:

  - Stateless (no conversation history).
  - Read-only (no action proposals).
  - Single LLM call per question.
  - Substring-based context selection (no embeddings).

The architecture is set up so each of those becomes its own future
slice: a session store for multi-turn; a tool-call dispatcher for
actions; an embedding-based retriever for richer context.

Context selection (`select_context_entries`) scores every ontology
entry by mentions in the question:

    +30 for a direct MPL label match (case-insensitive)
    +20 for a canonical_term word-boundary match (>= 4 chars)
    +15 for any alias word-boundary match (>= 4 chars)

A `focus_label` if supplied seeds the context with the focus + its
references + entries that reference it (the chat operator's
"zoom in on this entry" affordance).

The chat prompt inlines, for each context entry: parent, definitions
(most recent first, with attribution), references_labels, and the
current stale state with the last couple of stale_reasons. The
adapter answers in natural language; we parse MPL labels back out so
the response can cite specific entries.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from pymongo.database import Database

from mahalath.adapters.base import Adapter, AdapterError
from mahalath.db.models import OntologyEntry
from mahalath.db.repositories import OntologyEntryRepository


log = logging.getLogger("mahalath.chat")


_MPL_LABEL_PATTERN = re.compile(r"\bMPL-\d{3}(?:\.\d{3})*[a-z]?\b")
_MIN_TERM_LEN = 4


@dataclass
class ChatResponse:
    question: str
    answer: str
    context_labels: list[str]
    cited_labels: list[str]
    model_used: str
    duration_ms: int


# --- Context selection ----------------------------------------------------


def select_context_entries(
    question: str,
    db: Database,
    *,
    max_entries: int = 10,
    focus_label: str | None = None,
) -> list[OntologyEntry]:
    """Pick ontology entries most relevant to `question`."""
    entries_repo = OntologyEntryRepository(db)
    question_cf = question.casefold()

    scored: list[tuple[int, OntologyEntry]] = []
    seen_labels: set[str] = set()

    # Seed with focus_label + its 1-hop neighbourhood.
    if focus_label:
        focus = entries_repo.get(focus_label)
        if focus is not None:
            scored.append((1000, focus))
            seen_labels.add(focus.mpl_label)
            for ref_label in focus.references_labels[:5]:
                ref = entries_repo.get(ref_label)
                if ref and ref.mpl_label not in seen_labels:
                    scored.append((500, ref))
                    seen_labels.add(ref.mpl_label)
            # Reverse refs (callers of focus).
            from mahalath.staleness import entries_referencing
            for back_ref in entries_referencing(db, focus.mpl_label)[:5]:
                if back_ref.mpl_label not in seen_labels:
                    scored.append((400, back_ref))
                    seen_labels.add(back_ref.mpl_label)

    # Score remaining entries by mention in the question.
    for label in entries_repo.all_labels():
        if label in seen_labels:
            continue
        entry = entries_repo.get(label)
        if entry is None:
            continue

        score = 0
        if entry.mpl_label.casefold() in question_cf:
            score += 30

        term_cf = entry.canonical_term.casefold() if entry.canonical_term else ""
        if len(term_cf) >= _MIN_TERM_LEN and re.search(
            r"\b" + re.escape(term_cf) + r"\b", question_cf
        ):
            score += 20

        for alias in entry.aliases:
            alias_cf = alias.casefold()
            if len(alias_cf) >= _MIN_TERM_LEN and re.search(
                r"\b" + re.escape(alias_cf) + r"\b", question_cf
            ):
                score += 15

        if score > 0:
            scored.append((score, entry))
            seen_labels.add(label)

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [entry for _, entry in scored[:max_entries]]


# --- Prompt construction --------------------------------------------------


def build_chat_prompt(
    question: str,
    context_entries: list[OntologyEntry],
    *,
    style_overlay: str | None = None,
) -> str:
    parts: list[str] = []
    parts.append("You are an assistant for the Mahalath ontology.")
    parts.append("")
    parts.append(
        "The user is asking about the corpus. Below is the relevant subset "
        "of the live ontology. Answer the question grounded in these "
        "entries. Cite specific MPL labels in your prose so the user can "
        "click through. If an entry is flagged stale or has multiple "
        "definitions, note that explicitly. If the context doesn't cover "
        "the question, say so honestly rather than speculating."
    )
    parts.append("")

    if context_entries:
        parts.append("ONTOLOGY CONTEXT")
        parts.append("")
        for entry in context_entries:
            parts.extend(_render_entry_for_chat(entry))
            parts.append("")
    else:
        parts.append("ONTOLOGY CONTEXT")
        parts.append("(no entries in the ontology matched the question)")
        parts.append("")

    if style_overlay:
        from mahalath.style import render_style_block
        block = render_style_block(style_overlay)
        if block:
            parts.append(block)
            parts.append("")

    parts.append("USER QUESTION")
    parts.append(question)
    parts.append("")
    parts.append(
        "Answer in natural language, concise (2-6 sentences typical). "
        "Cite MPL labels inline like (MPL-001) when grounding claims."
    )
    return "\n".join(parts)


def _render_entry_for_chat(entry: OntologyEntry) -> list[str]:
    lines: list[str] = []
    lines.append(f"--- {entry.mpl_label}: {entry.canonical_term} ---")
    parent = entry.parent_label or "(top-level)"
    lines.append(f"  Parent: {parent}")
    if entry.is_stale:
        lines.append(f"  STALE (reasons: {len(entry.stale_reasons)})")
        for r in entry.stale_reasons[-2:]:
            note = r.get("note") or ""
            change_type = r.get("change_type", "?")
            lines.append(f"    - {change_type}: {note}")
    if entry.definitions:
        lines.append("  Definitions (most recent first):")
        for d in reversed(entry.definitions):
            attrib = d.model_used or "?"
            lines.append(f"    [{attrib}] {d.text}")
    if entry.references_labels:
        lines.append(
            f"  References: {', '.join(entry.references_labels[:8])}"
        )
    if entry.aliases:
        lines.append(f"  Aliases: {', '.join(entry.aliases[:5])}")
    return lines


# --- Citation extraction --------------------------------------------------


def extract_cited_labels(text: str) -> list[str]:
    """Pull MPL labels out of the answer text so the UI can deep-link."""
    return list(dict.fromkeys(_MPL_LABEL_PATTERN.findall(text)))


# --- Top-level API --------------------------------------------------------


def answer_question(
    question: str,
    db: Database,
    adapter: Adapter,
    *,
    max_context_entries: int = 10,
    focus_label: str | None = None,
    style_overlay: str | None = None,
) -> ChatResponse:
    """Answer `question` grounded in the live ontology."""
    context_entries = select_context_entries(
        question, db,
        max_entries=max_context_entries,
        focus_label=focus_label,
    )
    prompt = build_chat_prompt(
        question, context_entries, style_overlay=style_overlay
    )

    start = time.monotonic()
    try:
        response = adapter.generate(prompt, want_json=False)
    except AdapterError:
        raise
    duration_ms = int((time.monotonic() - start) * 1000)

    cited = extract_cited_labels(response.text)
    return ChatResponse(
        question=question,
        answer=response.text,
        context_labels=[e.mpl_label for e in context_entries],
        cited_labels=cited,
        model_used=response.model,
        duration_ms=duration_ms,
    )
