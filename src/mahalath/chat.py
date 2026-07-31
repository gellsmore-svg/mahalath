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

import json as _json
import logging
import re
import time
from dataclasses import dataclass

from pymongo.database import Database

from mahalath.adapters.base import Adapter, AdapterError
from mahalath.db.models import OntologyEntry
from mahalath.db.repositories import OntologyEntryRepository
from mahalath.retrieval import score_entry


log = logging.getLogger("mahalath.chat")


_MPL_LABEL_PATTERN = re.compile(r"\bMPL-\d{3}(?:\.\d{3})*[a-z]?\b")


@dataclass
class SuggestedAction:
    """An action the chat extracted from the conversation, pending operator confirmation."""

    type: str  # propose_parent | propose_alias  (first slice)
    payload: dict
    reasoning: str
    confidence: float


@dataclass
class ChatResponse:
    question: str
    answer: str
    context_labels: list[str]
    cited_labels: list[str]
    suggested_actions: list[SuggestedAction]
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

        score = score_entry(entry, question_cf)
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
    definition_contexts: dict | None = None,
    definition_intents: dict | None = None,
) -> str:
    """Build the chat prompt.

    `definition_contexts` (optional) is a {context_id → DefinitionContext}
    dict used to group an entry's multiple definitions by their frame.
    Per the polysemy design, multiple definitions with different contexts
    are co-equal — the prompt explicitly tells the model not to pick one
    "correct" definition, but to surface the relevant frame(s) for the
    question.
    """
    parts: list[str] = []
    parts.append("You are an assistant for the Mahalath ontology.")
    parts.append("")
    parts.append(
        "The user is asking about the corpus. Below is the relevant subset "
        "of the live ontology. Answer the question grounded in these "
        "entries. Cite specific MPL labels in your prose so the user can "
        "click through. If the context doesn't cover the question, say so "
        "honestly rather than speculating."
    )
    parts.append("")
    parts.append(
        "POLYSEMY NOTE: a single entry may carry MULTIPLE definitions, each "
        "speaking from a different context (theological, structural, "
        "physical, etc.). These are CO-EQUAL — none supersedes another. "
        "When a question implies a particular frame, surface the matching "
        "context's definition. When a question is frame-neutral, present "
        "multiple contexts explicitly (e.g., 'theologically, MPL-X is …; "
        "structurally, MPL-X is …')."
    )
    parts.append("")

    if context_entries:
        parts.append("ONTOLOGY CONTEXT")
        parts.append("")
        for entry in context_entries:
            parts.extend(_render_entry_for_chat(
                entry, definition_contexts, definition_intents,
            ))
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
    parts.append("OUTPUT FORMAT")
    parts.append(
        "Output ONLY a single JSON object of the form:\n"
        '{\n'
        '  "answer": "<your answer in natural language, 2-6 sentences. Cite MPL labels inline like (MPL-001).>",\n'
        '  "suggested_actions": [\n'
        '    { "type": "propose_parent",\n'
        '      "child_label": "<MPL-id>",\n'
        '      "parent_label": "<MPL-id>",\n'
        '      "reasoning": "<one sentence>",\n'
        '      "confidence": <0.0-10.0>\n'
        '    },\n'
        '    { "type": "propose_alias",\n'
        '      "label": "<MPL-id>",\n'
        '      "alias": "<text>",\n'
        '      "reasoning": "<one sentence>",\n'
        '      "confidence": <0.0-10.0>\n'
        '    }\n'
        '  ]\n'
        "}\n"
    )
    parts.append(
        "Only include suggested_actions when the user's question implies "
        "a concrete structural change (e.g. 'X should be a child of Y', "
        "'add an alias to Z'). When the user is just asking a question, "
        "return suggested_actions: []."
    )
    parts.append("No markdown fences, no preamble — just the JSON.")
    return "\n".join(parts)


def _render_entry_for_chat(
    entry: OntologyEntry,
    contexts: dict | None = None,
    intent_names: dict | None = None,
) -> list[str]:
    """Render an entry for the chat prompt via retrieval's shared renderer.

    `contexts` (optional) is a {context_id → DefinitionContext} map used
    to resolve frame names + descriptions; `intent_names` (optional) is
    a {context_id → name} map for intent tags (I-C). The actual text
    shaping lives in `retrieval.render_entry_lines` — the ONE renderer —
    so the chat context block and retrieval bundle text look identical
    to a downstream model (S-C).
    """
    from mahalath.retrieval import Meaning, render_entry_lines

    meanings = []
    ctx_descriptions: dict[str, str] = {}
    for d in entry.definitions:
        ctx = contexts.get(d.context_id) if contexts and d.context_id else None
        if ctx is not None:
            ctx_descriptions[ctx.name] = ctx.description
        meanings.append(Meaning(
            context_id=d.context_id,
            context_name=ctx.name if ctx else None,
            description=d.text,
            model_used=d.model_used,
            consensus_score=d.consensus_score,
            created_at=None,
            intent_tags=[
                (intent_names or {}).get(t, t) for t in d.intent_tags
            ],
            intentionality=d.intentionality,
            intent_confidence=d.intent_confidence,
        ))

    return render_entry_lines(
        mpl_label=entry.mpl_label,
        canonical_term=entry.canonical_term,
        meanings=meanings,
        parent_label=entry.parent_label,
        path=entry.path or None,
        references=entry.references_labels,
        aliases=entry.aliases,
        is_stale=entry.is_stale,
        stale_reasons=entry.stale_reasons,
        context_descriptions=ctx_descriptions or None,
    )


# --- Citation extraction --------------------------------------------------


def extract_cited_labels(text: str) -> list[str]:
    """Pull MPL labels out of the answer text so the UI can deep-link."""
    return list(dict.fromkeys(_MPL_LABEL_PATTERN.findall(text)))


# --- Response parsing ----------------------------------------------------


def parse_chat_envelope(text: str) -> tuple[str, list[SuggestedAction]]:
    """Parse the model's {answer, suggested_actions} JSON envelope.

    Tolerant of fenced JSON, surrounding prose, and missing
    suggested_actions. If the envelope can't be parsed at all, the raw
    text is treated as the answer and no actions are returned — so the
    chat keeps working even when the model doesn't follow the format.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```", 2)
        if len(parts) >= 2:
            inner = parts[1]
            if inner.startswith("json"):
                inner = inner[4:].lstrip()
            cleaned = inner

    obj: object
    try:
        obj = _json.loads(cleaned)
    except _json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end < 0 or end <= start:
            return cleaned, []
        try:
            obj = _json.loads(cleaned[start : end + 1])
        except _json.JSONDecodeError:
            return cleaned, []

    if not isinstance(obj, dict):
        return cleaned, []

    answer = str(obj.get("answer", "")).strip()
    if not answer:
        # Model returned the envelope shape but with empty answer;
        # fall back to the raw text so the user sees something.
        answer = cleaned

    raw_actions = obj.get("suggested_actions") or []
    if not isinstance(raw_actions, list):
        return answer, []

    actions: list[SuggestedAction] = []
    for raw in raw_actions:
        if not isinstance(raw, dict):
            continue
        atype = str(raw.get("type", "")).strip()
        if atype not in {"propose_parent", "propose_alias"}:
            continue
        try:
            confidence = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(10.0, confidence))
        reasoning = str(raw.get("reasoning", "")).strip()
        if atype == "propose_parent":
            payload = {
                "child_label": str(raw.get("child_label", "")).strip(),
                "parent_label": str(raw.get("parent_label", "")).strip(),
            }
            if not payload["child_label"] or not payload["parent_label"]:
                continue
        else:  # propose_alias
            payload = {
                "label": str(raw.get("label", "")).strip(),
                "alias": str(raw.get("alias", "")).strip(),
            }
            if not payload["label"] or not payload["alias"]:
                continue
        actions.append(SuggestedAction(
            type=atype, payload=payload,
            reasoning=reasoning, confidence=confidence,
        ))
    return answer, actions


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
    # Pull the definition_contexts table once and partition by kind:
    # frames group the definitions; intent names label the deployment
    # annotations (I-C). The two taxonomies never mix (ADR-024).
    from mahalath.db.repositories import DefinitionContextRepository
    all_contexts = DefinitionContextRepository(db).all()
    contexts_map = {
        c.context_id: c for c in all_contexts if c.kind == "frame"
    }
    intents_map = {
        c.context_id: c.name for c in all_contexts if c.kind == "intent"
    }

    prompt = build_chat_prompt(
        question, context_entries,
        style_overlay=style_overlay,
        definition_contexts=contexts_map if contexts_map else None,
        definition_intents=intents_map if intents_map else None,
    )

    start = time.monotonic()
    try:
        response = adapter.generate(prompt, want_json=True)
    except AdapterError:
        raise
    duration_ms = int((time.monotonic() - start) * 1000)

    answer, actions = parse_chat_envelope(response.text)
    cited = extract_cited_labels(answer)
    return ChatResponse(
        question=question,
        answer=answer,
        context_labels=[e.mpl_label for e in context_entries],
        cited_labels=cited,
        suggested_actions=actions,
        model_used=response.model,
        duration_ms=duration_ms,
    )
