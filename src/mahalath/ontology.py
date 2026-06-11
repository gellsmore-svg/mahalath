"""High-level ontology management: persist debate results into MongoDB.

This module is the seam between the pure-function debate loop and the
persistent ontology. Given a DebateResult it:

  - assigns an MPL label using the labels module (top-level by default,
    optionally under a chosen parent),
  - writes an OntologyEntry (flat dictionary) plus an OntologyTreeEdge
    when there is a parent,
  - writes the DecisionLogEntry recording the debate's outcome,
  - writes every AgentExchange so the per-iteration audit is queryable,
  - on undecided outcome, routes to the undecided queue with a reason
    derived from the run.

Contradistinct splits (DQ-005), inheritance of multiple parents,
relation edges other than child-of, and definition supersedence are
deferred to later stages; the schema accommodates them but Stage 1
exercises only the accepted/undecided base case.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pymongo.database import Database

from mahalath import labels
from mahalath.config import RuntimeConfig
from mahalath.db.models import (
    DecisionLogEntry,
    DefinitionVersion,
    OntologyEntry,
    OntologyTreeEdge,
    UndecidedItem,
)
from mahalath.db.repositories import (
    AgentExchangeRepository,
    DecisionLogRepository,
    OntologyEntryRepository,
    OntologyTreeRepository,
    UndecidedQueueRepository,
)
from mahalath.debate import DebateResult

ACCEPTED = "accepted"
UNDECIDED = "undecided"


@dataclass
class PersistResult:
    outcome: str
    decision_log_id: str
    ontology_entry: OntologyEntry | None = None
    undecided_item: UndecidedItem | None = None
    mpl_label: str | None = None


def persist_debate_result(
    result: DebateResult,
    db: Database,
    runtime: RuntimeConfig,
    *,
    parent_label: str | None = None,
    aliases: list[str] | None = None,
    extra_relations: list[dict[str, Any]] | None = None,
) -> PersistResult:
    """Persist a DebateResult into the ontology and audit collections.

    Caller may pin a `parent_label` (must be an existing accepted entry)
    or leave it None to assign at the top level. `aliases` and
    `extra_relations` flow through to the OntologyEntry record.
    """
    decision_repo = DecisionLogRepository(db)
    exchange_repo = AgentExchangeRepository(db)

    if result.outcome == ACCEPTED:
        ontology_entry, mpl_label = _write_accepted(
            result,
            db,
            parent_label=parent_label,
            aliases=aliases,
            extra_relations=extra_relations,
        )
        decision_repo.insert(
            _decision_log_entry(result, ACCEPTED, [mpl_label])
        )
        _persist_exchanges(result, exchange_repo)
        return PersistResult(
            outcome=ACCEPTED,
            decision_log_id=result.decision_log_id,
            ontology_entry=ontology_entry,
            mpl_label=mpl_label,
        )

    if result.outcome == UNDECIDED:
        decision_repo.insert(_decision_log_entry(result, UNDECIDED, []))
        _persist_exchanges(result, exchange_repo)
        item = _route_to_undecided(result, db, runtime)
        return PersistResult(
            outcome=UNDECIDED,
            decision_log_id=result.decision_log_id,
            undecided_item=item,
        )

    raise ValueError(
        f"persist_debate_result: unsupported outcome {result.outcome!r}; "
        "Stage 1 handles accepted and undecided only."
    )


def _write_accepted(
    result: DebateResult,
    db: Database,
    *,
    parent_label: str | None,
    aliases: list[str] | None,
    extra_relations: list[dict[str, Any]] | None,
) -> tuple[OntologyEntry, str]:
    if result.final_definition is None or result.final_confidence is None:
        raise ValueError(
            "Accepted debate result missing final_definition or final_confidence."
        )

    entries = OntologyEntryRepository(db)
    tree = OntologyTreeRepository(db)
    mpl_label = _assign_label(entries, parent_label=parent_label)

    context_id = _resolve_context_id(db, result.final_context_name)
    # Materialise the ancestor chain at insert: parent's own path plus
    # the parent itself (empty for a top-level entry). See paths.py.
    if parent_label is not None:
        from mahalath.paths import resolved_path
        parent_entry = entries.get(parent_label)
        entry_path = (
            [*resolved_path(db, parent_entry), parent_label]
            if parent_entry is not None
            else [parent_label]
        )
    else:
        entry_path = []
    entry = OntologyEntry(
        mpl_label=mpl_label,
        canonical_term=result.term,
        parent_label=parent_label,
        path=entry_path,
        confidence=result.final_confidence,
        definitions=[
            DefinitionVersion(
                text=result.final_definition,
                model_used=_model_used_in(result),
                decision_log_id=result.decision_log_id,
                context_id=context_id,
            )
        ],
        source_document_ids=[result.source_document_id],
        decision_log_id=result.decision_log_id,
        aliases=aliases or [],
        relations=extra_relations or [],
    )
    entries.insert(entry)

    if parent_label is not None:
        tree.add_edge(
            OntologyTreeEdge(parent_label=parent_label, child_label=mpl_label)
        )

    # Populate references_labels from the entry's definitions, so the
    # reverse-index is built incrementally as entries land — and
    # retro-link older entries whose definitions mention this new term,
    # so the reverse index doesn't go stale as the ontology grows.
    from mahalath.staleness import retro_link_new_entry, update_references
    update_references(db, mpl_label)
    retro_link_new_entry(db, mpl_label)

    return entry, mpl_label


def _assign_label(
    entries: OntologyEntryRepository, *, parent_label: str | None
) -> str:
    if parent_label is None:
        return labels.next_top_level(entries.all_labels())
    existing_children = entries.labels_under_parent(parent_label)
    return labels.next_child(parent_label, existing_children)


def _decision_log_entry(
    result: DebateResult, outcome: str, resulting_labels: list[str]
) -> DecisionLogEntry:
    return DecisionLogEntry(
        decision_log_id=result.decision_log_id,
        term=result.term,
        source_document_id=result.source_document_id,
        messages=result.messages,
        final_confidence=result.final_confidence,
        iterations_used=result.iterations_used,
        outcome=outcome,
        resulting_mpl_labels=resulting_labels,
    )


def _persist_exchanges(
    result: DebateResult, exchange_repo: AgentExchangeRepository
) -> None:
    for exchange in result.exchanges:
        exchange_repo.insert(exchange)


def _route_to_undecided(
    result: DebateResult, db: Database, runtime: RuntimeConfig
) -> UndecidedItem:
    reason = (
        "iteration_cap"
        if result.iterations_used >= runtime.max_iterations_per_term
        else "below_threshold"
    )
    item = UndecidedItem(
        decision_log_id=result.decision_log_id,
        term=result.term,
        source_document_id=result.source_document_id,
        reason=reason,
        context=result.context or None,
        last_confidence=result.final_confidence,
    )
    UndecidedQueueRepository(db).insert(item)
    return item


def persist_decision_audit(
    result: DebateResult,
    db: Database,
    *,
    outcome: str,
    resulting_labels: list[str] | None = None,
) -> None:
    """Write decision_log + agent_exchanges only.

    Used by REM re-review when re-debate produces another undecided
    outcome: we want the new attempt's audit trail to be queryable
    (per-iteration prompts/responses, final confidence, message
    history) but we do NOT want to create a duplicate
    UndecidedItem — the existing queue row is updated in place
    instead. Also useful for any future job that wants to record a
    debate run without changing ontology state.
    """
    DecisionLogRepository(db).insert(
        _decision_log_entry(result, outcome, resulting_labels or [])
    )
    _persist_exchanges(result, AgentExchangeRepository(db))


def _model_used_in(result: DebateResult) -> str | None:
    """Pick a representative model name from the recorded exchanges."""
    for exchange in reversed(result.exchanges):
        if exchange.model:
            return exchange.model
    return None


def _resolve_context_id(db: Database, context_name: str | None) -> str | None:
    """Map a context name (e.g., 'structural') to its DefinitionContext id."""
    if not context_name:
        return None
    from mahalath.db.repositories import DefinitionContextRepository
    ctx = DefinitionContextRepository(db).get_by_name(context_name)
    return ctx.context_id if ctx is not None else None
