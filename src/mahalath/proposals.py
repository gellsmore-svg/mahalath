"""Operator-facing workflow on action proposals.

The hierarchy reviewer + dispatcher can leave proposals in one of two
operator-actionable states:

  pending_review  — confidence below threshold, or destructive type, or
                    consensus not unanimous. Waiting for human accept
                    or reject.
  applied         — already in effect; can be rolled back if the
                    operator decides the autonomy decision was wrong.

This module provides:

  list_proposals(db, *, status=None) -> list[ActionProposal]
  get_proposal(proposal_id, db) -> ActionProposal
  accept_proposal(proposal_id, db, *, note=None) -> OperatorActionResult
  reject_proposal(proposal_id, db, *, note=None) -> OperatorActionResult
  rollback_proposal(proposal_id, db, *, note=None) -> OperatorActionResult

Every operator action writes back to the same ActionProposal record
with `operator_decision`, `operator_decision_at`, and optional
`operator_note`. The status transition is also recorded:

  pending_review -> applied        (accept)
  pending_review -> rejected       (reject)
  applied        -> rolled_back    (rollback)

Rollback reverses the changes recorded in `application_result`. For a
re-parenting parent action the previous tree edge is restored using
`previous_parent_label` captured at apply time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pymongo.database import Database

from mahalath.actions import (
    Action,
    ProposeAlias,
    ProposeMerge,
    ProposeParent,
    ProposeSplit,
    apply,
    validate,
)
from mahalath.db.models import ActionProposal, OntologyTreeEdge
from mahalath.db.repositories import (
    ActionProposalRepository,
    OntologyTreeRepository,
)
from mahalath.paths import propagate_paths


class ProposalError(Exception):
    """Raised when an operator action cannot be carried out."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class OperatorActionResult:
    proposal_id: str
    previous_status: str
    new_status: str
    detail: str
    application_result: dict[str, Any] | None = None
    rollback_result: dict[str, Any] | None = None


# --- Lookups ---------------------------------------------------------------


def get_proposal(proposal_id: str, db: Database) -> ActionProposal:
    proposal = ActionProposalRepository(db).get(proposal_id)
    if proposal is None:
        raise ProposalError(f"proposal not found: {proposal_id}")
    return proposal


def list_proposals(
    db: Database, *, status: str | None = None, limit: int = 100
) -> list[ActionProposal]:
    if status is not None:
        return ActionProposalRepository(db).by_status(status)
    return [
        ActionProposal.model_validate(doc)
        for doc in db.action_proposals.find().sort("created_at", 1).limit(limit)
    ]


# --- Operator decisions ----------------------------------------------------


def accept_proposal(
    proposal_id: str, db: Database, *, note: str | None = None,
    decided_via: str = "operator",
) -> OperatorActionResult:
    proposal = get_proposal(proposal_id, db)
    if proposal.status != "pending_review":
        raise ProposalError(
            f"cannot accept proposal in status {proposal.status!r}; "
            "only pending_review proposals are eligible"
        )

    # The world may have moved since the proposal was made — re-validate
    # at apply time so we don't quietly land a stale change.
    action = _rehydrate_action(proposal)
    validation = validate(action, db)
    if not validation.valid:
        raise ProposalError(
            f"proposal can no longer be applied: {validation.reason}"
        )

    application_result = apply(action, db)
    now = _utcnow()
    db.action_proposals.update_one(
        {"proposal_id": proposal_id},
        {
            "$set": {
                "status": "applied",
                "applied_at": now,
                "operator_decision": "accepted",
                "operator_decision_at": now,
                "decided_via": decided_via,
                "operator_note": note,
                "application_result": application_result,
                "rejection_reason": None,
            }
        },
    )
    return OperatorActionResult(
        proposal_id=proposal_id,
        previous_status="pending_review",
        new_status="applied",
        detail="operator accepted; action applied",
        application_result=application_result,
    )


def reject_proposal(
    proposal_id: str, db: Database, *, note: str | None = None,
    decided_via: str = "operator",
) -> OperatorActionResult:
    proposal = get_proposal(proposal_id, db)
    if proposal.status != "pending_review":
        raise ProposalError(
            f"cannot reject proposal in status {proposal.status!r}"
        )

    now = _utcnow()
    db.action_proposals.update_one(
        {"proposal_id": proposal_id},
        {
            "$set": {
                "status": "rejected",
                "operator_decision": "rejected",
                "operator_decision_at": now,
                "decided_via": decided_via,
                "operator_note": note,
            }
        },
    )
    return OperatorActionResult(
        proposal_id=proposal_id,
        previous_status="pending_review",
        new_status="rejected",
        detail="operator rejected",
    )


def rollback_proposal(
    proposal_id: str, db: Database, *, note: str | None = None,
    decided_via: str = "operator",
) -> OperatorActionResult:
    proposal = get_proposal(proposal_id, db)
    if proposal.status != "applied":
        raise ProposalError(
            f"cannot rollback proposal in status {proposal.status!r}; "
            "only applied proposals are eligible"
        )

    rollback_result = _rollback(proposal, db)
    # A rollback is a structural undo; dependents may need to be
    # re-evaluated against the restored state.
    affected_label = proposal.payload.get("child_label") or proposal.payload.get("label")
    if affected_label:
        from mahalath.staleness import mark_dependents_stale
        mark_dependents_stale(
            db, affected_label,
            change_type=f"rollback_{proposal.action_type}",
            note=note,
        )
    now = _utcnow()

    # Preserve the original application_result alongside the rollback
    # outcome so the audit chain is fully reconstructable.
    new_application_result = {
        **proposal.application_result,
        "rollback_result": rollback_result,
    }
    db.action_proposals.update_one(
        {"proposal_id": proposal_id},
        {
            "$set": {
                "status": "rolled_back",
                "operator_decision": "rolled_back",
                "operator_decision_at": now,
                "decided_via": decided_via,
                "operator_note": note,
                "application_result": new_application_result,
            }
        },
    )
    return OperatorActionResult(
        proposal_id=proposal_id,
        previous_status="applied",
        new_status="rolled_back",
        detail="operator rolled back",
        rollback_result=rollback_result,
    )


# --- Internals -------------------------------------------------------------


def _rehydrate_action(proposal: ActionProposal) -> Action:
    payload = proposal.payload
    common = {
        "reason": proposal.reason,
        "confidence": proposal.confidence,
        "proposed_by": proposal.proposed_by,
        "source_decision_log_id": proposal.source_decision_log_id,
        "source_ontology_review_id": proposal.source_ontology_review_id,
    }
    if proposal.action_type == "propose_parent":
        return ProposeParent(
            child_label=str(payload.get("child_label", "")),
            parent_label=str(payload.get("parent_label", "")),
            **common,
        )
    if proposal.action_type == "propose_alias":
        return ProposeAlias(
            label=str(payload.get("label", "")),
            alias=str(payload.get("alias", "")),
            **common,
        )
    if proposal.action_type == "propose_merge":
        return ProposeMerge(
            keep_label=str(payload.get("keep_label", "")),
            drop_label=str(payload.get("drop_label", "")),
            **common,
        )
    if proposal.action_type == "propose_split":
        into_raw = payload.get("into", [])
        if not isinstance(into_raw, list):
            into_raw = []
        return ProposeSplit(
            label=str(payload.get("label", "")),
            into=tuple(str(x) for x in into_raw),
            **common,
        )
    raise ProposalError(f"unknown action type: {proposal.action_type!r}")


def _rollback(proposal: ActionProposal, db: Database) -> dict[str, Any]:
    if proposal.action_type == "propose_parent":
        return _rollback_parent(proposal, db)
    if proposal.action_type == "propose_alias":
        return _rollback_alias(proposal, db)
    raise ProposalError(
        f"rollback is not implemented for action type "
        f"{proposal.action_type!r} (destructive actions never auto-apply, "
        "so they have no rollback path)"
    )


def _rollback_parent(
    proposal: ActionProposal, db: Database
) -> dict[str, Any]:
    payload = proposal.payload
    application_result = proposal.application_result
    child_label = payload["child_label"]
    parent_label = payload["parent_label"]
    previous_parent_label = application_result.get("previous_parent_label")

    db.ontology_tree.delete_one(
        {"parent_label": parent_label, "child_label": child_label}
    )

    if previous_parent_label:
        OntologyTreeRepository(db).add_edge(
            OntologyTreeEdge(
                parent_label=previous_parent_label,
                child_label=child_label,
                relation_type="child_of",
            )
        )
        db.ontology_entries.update_one(
            {"_id": child_label},
            {
                "$set": {
                    "parent_label": previous_parent_label,
                    "updated_at": _utcnow(),
                }
            },
        )
        propagate_paths(db, child_label)
        return {
            "tree_edge_removed": True,
            "tree_edge_restored": True,
            "child_label": child_label,
            "restored_parent_label": previous_parent_label,
        }

    db.ontology_entries.update_one(
        {"_id": child_label},
        {"$set": {"parent_label": None, "updated_at": _utcnow()}},
    )
    propagate_paths(db, child_label)
    return {
        "tree_edge_removed": True,
        "parent_label_cleared": True,
        "child_label": child_label,
    }


def _rollback_alias(
    proposal: ActionProposal, db: Database
) -> dict[str, Any]:
    payload = proposal.payload
    application_result = proposal.application_result
    label = payload["label"]
    alias = application_result.get("alias_added") or payload.get("alias")
    if alias:
        db.ontology_entries.update_one(
            {"_id": label},
            {
                "$pull": {"aliases": alias},
                "$set": {"updated_at": _utcnow()},
            },
        )
    return {"alias_removed": alias, "label": label}
