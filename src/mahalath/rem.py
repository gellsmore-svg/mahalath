"""REM re-review of the undecided queue.

When run_debate ends inconclusively a term lands in `undecided_queue`
with `escalation_level=0`. The REM job picks those up, re-runs debate
with the current model + per-document style overlay + (potentially)
different runtime knobs, and either promotes the term into the
ontology or bumps its `escalation_level`. Items past `max_escalation`
are skipped entirely — they need an operator decision, not more model
spend.

Idempotence model: one undecided queue row per term. A re-debate that
stays undecided UPDATES the existing row (last_confidence,
escalation_level) and writes a fresh DecisionLogEntry + AgentExchange
audit chain for the new attempt. A re-debate that accepts removes the
row, writes a new ontology entry, and the audit trail of the original
undecided run is reachable through the original decision_log_id (no
data is dropped).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pymongo.database import Database

from mahalath.adapters.base import Adapter
from mahalath.config import AppConfig
from mahalath.db.repositories import (
    DocumentRepository,
    UndecidedQueueRepository,
)
from mahalath.debate import DebateError, run_debate
from mahalath.ontology import persist_debate_result, persist_decision_audit
from mahalath.style import resolve_style_overlay


log = logging.getLogger("mahalath.rem")


@dataclass
class RemReviewResult:
    items_pending_at_start: int = 0
    items_reviewed: int = 0
    items_accepted: int = 0
    items_still_undecided: int = 0
    items_skipped_max_escalation: int = 0
    items_errored: int = 0
    accepted_labels: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def rem_review(
    config: AppConfig,
    db: Database,
    adapter: Adapter,
    *,
    max_items: int = 10,
    max_escalation: int = 3,
) -> RemReviewResult:
    """Walk the undecided queue and re-run debate on eligible items."""
    queue = UndecidedQueueRepository(db)
    documents = DocumentRepository(db)

    # Read enough to backfill items skipped because they're at max
    # escalation; cap the actual model spend at max_items.
    pending = queue.list_pending(limit=max(max_items * 4, 32))
    result = RemReviewResult(items_pending_at_start=len(pending))


    from mahalath.db.repositories import DefinitionContextRepository

    available_contexts = [
        {"name": c.name, "description": c.description}
        for c in DefinitionContextRepository(db).all()
    ]
    for item in pending:
        if result.items_reviewed >= max_items:
            break
        if item.escalation_level >= max_escalation:
            result.items_skipped_max_escalation += 1
            continue
        if not item.context:
            # Pre-S2.13 queue rows that don't carry the original context
            # cannot be safely re-debated; leave them for operator action.
            result.items_skipped_max_escalation += 1
            continue

        document = documents.find_by_document_id(item.source_document_id)
        overlay = resolve_style_overlay(document, config)

        try:
            debate_result = run_debate(
                term=item.term,
                context=item.context,
                source_document_id=item.source_document_id,
                adapter=adapter,
                runtime=config.runtime,
                style_overlay=overlay,
                available_contexts=available_contexts or None,
            )
        except DebateError as exc:
            result.items_errored += 1
            result.errors.append(f"{item.term!r}: {exc}")
            continue

        result.items_reviewed += 1

        if debate_result.outcome == "accepted":
            persist_result = persist_debate_result(
                debate_result, db, config.runtime
            )
            # Remove the queue row — the term is now ontology.
            db.undecided_queue.delete_one(
                {"decision_log_id": item.decision_log_id}
            )
            result.items_accepted += 1
            if persist_result.mpl_label:
                result.accepted_labels.append(persist_result.mpl_label)
            log.info(
                "REM: accepted %r → %s (conf %s, iter %d)",
                item.term,
                persist_result.mpl_label,
                debate_result.final_confidence,
                debate_result.iterations_used,
            )
            continue

        # Still undecided: record the new attempt's audit chain and
        # bump the existing queue row in place.
        persist_decision_audit(
            debate_result, db, outcome=debate_result.outcome,
        )
        db.undecided_queue.update_one(
            {"decision_log_id": item.decision_log_id},
            {"$set": {
                "last_confidence": debate_result.final_confidence,
                "escalation_level": item.escalation_level + 1,
            }},
        )
        result.items_still_undecided += 1
        log.info(
            "REM: still undecided %r (conf %s, escalation %d)",
            item.term,
            debate_result.final_confidence,
            item.escalation_level + 1,
        )

    return result
