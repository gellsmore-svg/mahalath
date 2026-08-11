"""Read back the conversations that produced a term's prose (ADR-034).

Every model call that contributes prose to an entry writes a `decision_log`
row plus its `agent_exchanges` — the debate that produced the short
authoritative `text`, and (since ADR-034) the expansion that produced
`detailed_text`. The records were always written for the debate; what was
missing was any way to read them without querying MongoDB by hand.

This module is that reading layer. It gathers every conversation attached to
an entry, labels each one by which prose layer it produced, and renders it
for the CLI and the web UI. It is strictly read-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pymongo.database import Database

from mahalath.db.models import ELABORATED, AgentExchange, DecisionLogEntry
from mahalath.db.repositories import (
    AgentExchangeRepository,
    DecisionLogRepository,
    OntologyEntryRepository,
)

# Which prose layer a conversation produced.
LAYER_DEBATE = "debate"          # the short, authoritative `text`
LAYER_EXPOSITION = "exposition"  # `detailed_text`
LAYER_SCHOLARLY = "scholarly"    # reserved (ADR-033)

_ROLE_LAYERS = {
    "detailed_exposition": LAYER_EXPOSITION,
    "scholarly": LAYER_SCHOLARLY,
}


@dataclass
class Conversation:
    """One recorded model conversation, with what it produced."""

    decision_log_id: str
    layer: str
    term: str
    outcome: str
    source_document_id: str
    created_at: datetime | None = None
    final_confidence: float | None = None
    iterations_used: int = 0
    # Which definition slot this conversation produced prose for, when known.
    definition_index: int | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    exchanges: list[AgentExchange] = field(default_factory=list)
    # True when the id is referenced by a definition but the row is gone —
    # says "this predates capture" rather than silently showing nothing.
    missing: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_log_id": self.decision_log_id,
            "layer": self.layer,
            "term": self.term,
            "outcome": self.outcome,
            "source_document_id": self.source_document_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "final_confidence": self.final_confidence,
            "iterations_used": self.iterations_used,
            "definition_index": self.definition_index,
            "missing": self.missing,
            "messages": list(self.messages),
            "exchanges": [e.model_dump() for e in self.exchanges],
        }


def _layer_for(log: DecisionLogEntry, exchanges: list[AgentExchange]) -> str:
    if log.outcome == ELABORATED:
        return LAYER_EXPOSITION
    for source in (exchanges, log.messages):
        for item in source:
            role = getattr(item, "role", None)
            if role in _ROLE_LAYERS:
                return _ROLE_LAYERS[role]
    return LAYER_DEBATE


def load_conversation(
    db: Database,
    decision_log_id: str,
    *,
    definition_index: int | None = None,
) -> Conversation | None:
    """Load one conversation by id, or None when no such record exists."""
    log = DecisionLogRepository(db).get(decision_log_id)
    if log is None:
        return None
    exchanges = AgentExchangeRepository(db).for_decision(decision_log_id)
    return Conversation(
        decision_log_id=decision_log_id,
        layer=_layer_for(log, exchanges),
        term=log.term,
        outcome=log.outcome,
        source_document_id=log.source_document_id,
        created_at=log.created_at,
        final_confidence=log.final_confidence,
        iterations_used=log.iterations_used,
        definition_index=definition_index,
        messages=[m.model_dump() for m in log.messages],
        exchanges=exchanges,
    )


def conversations_for_entry(db: Database, mpl_label: str) -> list[Conversation]:
    """Every conversation behind an entry's prose, oldest first.

    Covers the entry-level debate and, per definition, both the debate that
    produced its `text` and the expansion that produced its `detailed_text`.
    Ids referenced by a definition whose record is absent are returned with
    ``missing=True`` — prose generated before ADR-034 has no conversation, and
    saying so is better than showing an empty page.
    """
    entry = OntologyEntryRepository(db).get(mpl_label)
    if entry is None:
        return []

    # (decision_log_id, definition_index) in the order we want them offered.
    wanted: list[tuple[str, int | None]] = []
    seen: set[str] = set()

    def _add(log_id: str | None, index: int | None) -> None:
        if not log_id or log_id in seen:
            return
        seen.add(log_id)
        wanted.append((log_id, index))

    _add(entry.decision_log_id, None)
    for index, definition in enumerate(entry.definitions):
        _add(definition.decision_log_id, index)
        _add(getattr(definition, "detailed_decision_log_id", None), index)

    out: list[Conversation] = []
    for log_id, index in wanted:
        conversation = load_conversation(db, log_id, definition_index=index)
        if conversation is None:
            out.append(
                Conversation(
                    decision_log_id=log_id,
                    layer=LAYER_DEBATE,
                    term=entry.canonical_term,
                    outcome="unknown",
                    source_document_id="",
                    definition_index=index,
                    missing=True,
                )
            )
        else:
            out.append(conversation)
    out.sort(key=_ordering_key)
    return out


def _ordering_key(conversation: Conversation) -> tuple[int, float]:
    """Oldest first, undated last.

    MongoDB returns naive UTC datetimes while freshly-built records are
    timezone-aware, so comparing them directly raises. Normalise to a POSIX
    timestamp and treat a naive value as UTC, which is what it is.
    """
    stamp = conversation.created_at
    if stamp is None:
        return (1, 0.0)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (0, stamp.timestamp())


def render_conversation(conversation: Conversation, *, verbose: bool = False) -> str:
    """Human-readable transcript. ``verbose`` includes full prompts."""
    if conversation.missing:
        return (
            f"{conversation.decision_log_id}  (no record — this prose predates "
            "conversation capture, ADR-034)"
        )
    head = [
        f"{conversation.decision_log_id}  [{conversation.layer}]  "
        f"{conversation.term!r}",
        f"  outcome: {conversation.outcome}"
        + (
            f"   confidence: {conversation.final_confidence}"
            if conversation.final_confidence is not None
            else ""
        )
        + (
            f"   iterations: {conversation.iterations_used}"
            if conversation.iterations_used
            else ""
        ),
        f"  document: {conversation.source_document_id or '—'}"
        + (
            f"   definition #{conversation.definition_index}"
            if conversation.definition_index is not None
            else ""
        ),
        "",
    ]
    body: list[str] = []
    for exchange in conversation.exchanges:
        body.append(
            f"  ── iteration {exchange.iteration} · {exchange.role} "
            f"· {exchange.model}"
            + (
                f" · confidence {exchange.confidence}"
                if exchange.confidence is not None
                else ""
            )
        )
        if verbose:
            body.append("     PROMPT:")
            body.extend(f"       {line}" for line in exchange.prompt.splitlines())
        body.append("     RESPONSE:")
        body.extend(f"       {line}" for line in exchange.response.splitlines())
        body.append("")
    if not conversation.exchanges:
        for message in conversation.messages:
            body.append(
                f"  ── {message.get('role')} · {message.get('model') or '?'}"
            )
            body.extend(
                f"       {line}"
                for line in str(message.get("content") or "").splitlines()
            )
            body.append("")
        if not conversation.messages:
            body.append("  (record exists but carries no messages)")
    return "\n".join(head + body).rstrip()
