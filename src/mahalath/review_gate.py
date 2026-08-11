"""Which undecided terms actually need a human (ADR-037).

The operator should be asked to review a term only when the system has
finished trying and confidence is still short. Anything that resolves on
re-debate must never reach a person.

The gate is on ATTEMPTS, not on a second confidence number: a term below
``runtime.confidence_threshold`` re-debates overnight, and only surfaces once
it has been attempted enough times that further recursion is not going to move
it. Two exceptions run the other way — a `conflict` or a `moderator_block` is
a structural disagreement (does this term hold one meaning or two?) that
re-debate does not resolve, so it goes to the operator immediately.

The rule is expressed twice on purpose: :func:`needs_operator_review` is the
readable definition, and :func:`review_query` is the same rule as a Mongo
filter so the queue does not have to be pulled into memory. A test asserts the
two agree — if they ever diverge, the divergence is the bug.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pymongo.database import Database

from mahalath.db.models import UndecidedItem

# Attempts (initial debate + this many re-debates) before a still-short term
# is worth a person's time. The re-debate runs the same two agents over the
# same source snippet, so a third identical pass rarely moves a score that has
# not moved twice. Raise it if the effectiveness report shows items resolving
# at escalation 2-3 — that data is already collected.
REVIEW_ESCALATION_THRESHOLD = 2

# Structural disagreements: more recursion does not settle them.
IMMEDIATE_REASONS: frozenset[str] = frozenset({"conflict", "moderator_block"})

# Enqueued without a debate (retrieval.propose_term). Never surfaced until it
# has actually been debated at least once — there is nothing to review yet.
REQUIRES_A_DEBATE_FIRST: frozenset[str] = frozenset({"proposed_term"})


def needs_operator_review(
    item: UndecidedItem, *, confidence_threshold: float
) -> bool:
    """True when this queued term should be shown to the operator."""
    if item.reason in IMMEDIATE_REASONS:
        return True
    if item.reason in REQUIRES_A_DEBATE_FIRST and item.last_confidence is None:
        return False
    if item.escalation_level < REVIEW_ESCALATION_THRESHOLD:
        return False
    # An unscored item that has been retried to the ceiling is stuck for some
    # other reason; still the operator's problem.
    if item.last_confidence is None:
        return True
    return item.last_confidence < confidence_threshold


def review_query(*, confidence_threshold: float) -> dict[str, Any]:
    """The same rule as a Mongo filter (see module docstring)."""
    immediate = sorted(IMMEDIATE_REASONS)
    needs_debate = sorted(REQUIRES_A_DEBATE_FIRST)
    exhausted: dict[str, Any] = {
        "reason": {"$nin": immediate},
        "escalation_level": {"$gte": REVIEW_ESCALATION_THRESHOLD},
        "$or": [
            {"last_confidence": None},
            {"last_confidence": {"$lt": confidence_threshold}},
        ],
    }
    return {
        "$or": [
            {"reason": {"$in": immediate}},
            {
                "$and": [
                    exhausted,
                    {
                        "$or": [
                            {"reason": {"$nin": needs_debate}},
                            {"last_confidence": {"$ne": None}},
                        ]
                    },
                ]
            },
        ]
    }


@dataclass
class ReviewQueue:
    """What the operator is being asked to look at, and what is still in flight."""

    awaiting: list[UndecidedItem]
    still_retrying: int
    total_pending: int
    confidence_threshold: float
    escalation_threshold: int = REVIEW_ESCALATION_THRESHOLD

    def to_dict(self) -> dict[str, Any]:
        return {
            "awaiting_review": [i.model_dump() for i in self.awaiting],
            "awaiting_count": len(self.awaiting),
            "still_retrying": self.still_retrying,
            "total_pending": self.total_pending,
            "confidence_threshold": self.confidence_threshold,
            "escalation_threshold": self.escalation_threshold,
        }


def load_review_queue(
    db: Database, *, confidence_threshold: float, limit: int = 200
) -> ReviewQueue:
    """Items needing a person, plus a count of those still being retried.

    Reporting `still_retrying` matters: an empty review list means "nothing
    needs you", not "nothing is happening", and the difference should be
    visible rather than inferred.
    """
    query = review_query(confidence_threshold=confidence_threshold)
    cursor = (
        db.undecided_queue.find(query)
        .sort([("escalation_level", -1), ("created_at", 1)])
        .limit(limit)
    )
    awaiting = [UndecidedItem.model_validate(doc) for doc in cursor]
    total = db.undecided_queue.count_documents({})
    return ReviewQueue(
        awaiting=awaiting,
        still_retrying=max(0, total - db.undecided_queue.count_documents(query)),
        total_pending=total,
        confidence_threshold=confidence_threshold,
    )


# --- Operator actions (ADR-037) -------------------------------------------
#
# Until now `/undecided` could only be looked at. These are the two decisions
# a person can make about a term the system could not settle, and both write
# to the same audit chain the proposals queue uses, so an operator decision is
# as traceable as a model one.


class ReviewError(Exception):
    """Raised when an undecided item cannot be acted on."""


def _take_item(db: Database, decision_log_id: str) -> UndecidedItem:
    doc = db.undecided_queue.find_one({"decision_log_id": decision_log_id})
    if doc is None:
        raise ReviewError(f"no undecided item for {decision_log_id!r}")
    return UndecidedItem.model_validate(doc)


def _last_proposed_definition(db: Database, decision_log_id: str) -> str | None:
    """The definition the debate ended on, for the operator to accept as-is."""
    log = db.decision_log.find_one({"decision_log_id": decision_log_id})
    if not log:
        return None
    import json

    for message in reversed(log.get("messages") or []):
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        try:
            parsed = json.loads(content)
        except (ValueError, TypeError):
            return content
        if isinstance(parsed, dict) and parsed.get("definition"):
            return str(parsed["definition"]).strip()
    return None


def accept_undecided(
    db: Database,
    decision_log_id: str,
    runtime: Any,
    *,
    definition: str | None = None,
    parent_label: str | None = None,
    note: str = "",
    decided_by: str = "operator",
) -> str:
    """Accept a stuck term into the ontology on the operator's authority.

    ``definition`` overrides the one the debate ended on. Returns the new MPL
    label. The entry is written through the normal persist path so references,
    paths and the reverse index are maintained exactly as for a model accept —
    an operator decision must not produce a differently-shaped entry.
    """
    from uuid import uuid4

    from mahalath.db.models import DebateMessage
    from mahalath.debate import DebateResult
    from mahalath.ontology import persist_debate_result

    item = _take_item(db, decision_log_id)
    text = (definition or _last_proposed_definition(db, decision_log_id) or "").strip()
    if not text:
        raise ReviewError(
            f"{decision_log_id!r} has no definition to accept; supply one explicitly"
        )

    # The operator's acceptance is a NEW decision, not a rewrite of the debate
    # that failed to settle: it gets its own log id (which `decision_log_id` is
    # uniquely indexed on anyway) and names the debate it overrode, so both are
    # readable side by side via ADR-034.
    accept_log_id = str(uuid4())
    result = DebateResult(
        decision_log_id=accept_log_id,
        term=item.term,
        source_document_id=item.source_document_id,
        outcome="accepted",
        final_definition=text,
        # The operator's decision is the authority here, not a model score;
        # confidence records what the debate reached, which may be below the
        # threshold — that is the whole reason this needed a person.
        final_confidence=item.last_confidence
        if item.last_confidence is not None
        else float(getattr(runtime, "confidence_threshold", 8.0)),
        iterations_used=0,
        context=item.context or "",
        messages=[
            DebateMessage(
                iteration=1,
                role="operator",
                content=(
                    f"Accepted by {decided_by} after {item.escalation_level} "
                    f"re-debate(s) left it at "
                    f"{item.last_confidence if item.last_confidence is not None else 'no score'} "
                    f"(reason: {item.reason}). Overrides debate "
                    f"{decision_log_id}."
                    + (f" Note: {note}" if note else "")
                ),
                confidence=item.last_confidence,
                model="operator",
            )
        ],
    )
    persisted = persist_debate_result(result, db, runtime, parent_label=parent_label)
    db.undecided_queue.delete_one({"decision_log_id": decision_log_id})
    _record_operator_decision(
        db, item, action="accept", note=note, decided_by=decided_by,
        resulting_mpl_label=persisted.mpl_label,
    )
    return persisted.mpl_label or ""


def reject_undecided(
    db: Database,
    decision_log_id: str,
    *,
    note: str = "",
    decided_by: str = "operator",
) -> None:
    """Drop a stuck term from the queue. The audit trail keeps the reasoning."""
    item = _take_item(db, decision_log_id)
    db.undecided_queue.delete_one({"decision_log_id": decision_log_id})
    _record_operator_decision(
        db, item, action="reject", note=note, decided_by=decided_by,
    )


def _record_operator_decision(
    db: Database,
    item: UndecidedItem,
    *,
    action: str,
    note: str,
    decided_by: str,
    resulting_mpl_label: str | None = None,
) -> None:
    from datetime import datetime, timezone

    db.operator_decisions.insert_one(
        {
            "decision_log_id": item.decision_log_id,
            "term": item.term,
            "source_document_id": item.source_document_id,
            "action": action,
            "note": note,
            "decided_by": decided_by,
            "decided_at": datetime.now(timezone.utc),
            "queue_reason": item.reason,
            "last_confidence": item.last_confidence,
            "escalation_level": item.escalation_level,
            "resulting_mpl_label": resulting_mpl_label,
        }
    )
