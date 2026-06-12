"""Operator-workflow tests against a live MongoDB test db.

Covers accept (with revalidation), reject, rollback (both initial
parent and re-parenting cases, plus alias), and the error paths
(missing proposal, wrong status, stale validation).
"""

from __future__ import annotations

import pytest

from mahalath.actions import ProposeAlias, ProposeParent, dispatch
from mahalath.db.models import (
    ActionProposal,
    DefinitionVersion,
    OntologyEntry,
    OntologyTreeEdge,
)
from mahalath.db.repositories import (
    ActionProposalRepository,
    OntologyEntryRepository,
    OntologyTreeRepository,
)
from mahalath.proposals import (
    ProposalError,
    accept_proposal,
    get_proposal,
    list_proposals,
    reject_proposal,
    rollback_proposal,
)


def _seed(mongo_db) -> None:
    repo = OntologyEntryRepository(mongo_db)
    repo.insert(OntologyEntry(
        mpl_label="MPL-001", canonical_term="Substrate", confidence=8.0,
        definitions=[DefinitionVersion(text="Underlying medium.")],
    ))
    repo.insert(OntologyEntry(
        mpl_label="MPL-002", canonical_term="Relational Substrate",
        confidence=8.5,
        definitions=[DefinitionVersion(text="Relational variant.")],
    ))
    repo.insert(OntologyEntry(
        mpl_label="MPL-003", canonical_term="Continuity", confidence=8.0,
        definitions=[DefinitionVersion(text="No gaps.")],
    ))


def _pending_parent_proposal(mongo_db, child="MPL-002", parent="MPL-001",
                              conf=7.0) -> ActionProposal:
    """Insert a pending_review propose_parent proposal directly."""
    action = ProposeParent(
        child_label=child, parent_label=parent,
        reason="test pending", confidence=conf,
    )
    # Dispatch with a high threshold so it lands in pending_review.
    result = dispatch(action, mongo_db, auto_apply_threshold=9.0)
    assert result.status == "pending_review"
    return ActionProposalRepository(mongo_db).get(result.proposal_id)


# --- Lookups ---------------------------------------------------------------


def test_list_proposals_returns_all(mongo_db) -> None:
    _seed(mongo_db)
    _pending_parent_proposal(mongo_db, "MPL-002", "MPL-001")
    _pending_parent_proposal(mongo_db, "MPL-003", "MPL-001")
    assert len(list_proposals(mongo_db)) == 2


def test_list_proposals_filters_by_status(mongo_db) -> None:
    _seed(mongo_db)
    _pending_parent_proposal(mongo_db, "MPL-002", "MPL-001")
    # Apply one (high confidence so it auto-applies)
    dispatch(
        ProposeParent(
            child_label="MPL-003", parent_label="MPL-001",
            reason="x", confidence=9.5,
        ),
        mongo_db,
    )
    assert len(list_proposals(mongo_db, status="pending_review")) == 1
    assert len(list_proposals(mongo_db, status="applied")) == 1


def test_get_proposal_raises_on_missing(mongo_db) -> None:
    with pytest.raises(ProposalError):
        get_proposal("does-not-exist", mongo_db)


# --- Accept ----------------------------------------------------------------


def test_accept_proposal_applies_and_records_decision(mongo_db) -> None:
    _seed(mongo_db)
    proposal = _pending_parent_proposal(
        mongo_db, "MPL-002", "MPL-001", conf=7.0
    )
    result = accept_proposal(
        proposal.proposal_id, mongo_db, note="manually verified"
    )
    assert result.new_status == "applied"

    # Side effect happened
    entry = OntologyEntryRepository(mongo_db).get("MPL-002")
    assert entry.parent_label == "MPL-001"
    assert OntologyTreeRepository(mongo_db).children_of("MPL-001") == ["MPL-002"]

    # Audit captures operator decision
    updated = get_proposal(proposal.proposal_id, mongo_db)
    assert updated.status == "applied"
    assert updated.operator_decision == "accepted"
    assert updated.operator_note == "manually verified"
    assert updated.operator_decision_at is not None
    assert updated.applied_at is not None
    assert updated.application_result["tree_edge_added"] is True


def test_accept_proposal_rejects_non_pending(mongo_db) -> None:
    _seed(mongo_db)
    # Insert directly with status=applied
    proposal = ActionProposal(
        action_type="propose_parent",
        payload={"child_label": "MPL-002", "parent_label": "MPL-001"},
        reason="x", confidence=9.0, status="applied",
    )
    ActionProposalRepository(mongo_db).insert(proposal)
    with pytest.raises(ProposalError):
        accept_proposal(proposal.proposal_id, mongo_db)


def test_accept_proposal_rejects_stale_validation(mongo_db) -> None:
    """If the world has changed (label gone), accept should refuse."""
    _seed(mongo_db)
    proposal = _pending_parent_proposal(
        mongo_db, "MPL-002", "MPL-001", conf=7.0
    )
    # Remove the parent label.
    mongo_db.ontology_entries.delete_one({"_id": "MPL-001"})
    with pytest.raises(ProposalError) as exc_info:
        accept_proposal(proposal.proposal_id, mongo_db)
    assert "no longer be applied" in str(exc_info.value)


# --- Reject ----------------------------------------------------------------


def test_reject_proposal_records_decision(mongo_db) -> None:
    _seed(mongo_db)
    proposal = _pending_parent_proposal(
        mongo_db, "MPL-002", "MPL-001", conf=7.0
    )
    result = reject_proposal(
        proposal.proposal_id, mongo_db, note="wrong direction"
    )
    assert result.new_status == "rejected"

    updated = get_proposal(proposal.proposal_id, mongo_db)
    assert updated.status == "rejected"
    assert updated.operator_decision == "rejected"
    assert updated.operator_note == "wrong direction"

    # Side effect did NOT happen
    entry = OntologyEntryRepository(mongo_db).get("MPL-002")
    assert entry.parent_label is None


# --- Rollback --------------------------------------------------------------


def test_rollback_initial_parent_clears_edge_and_parent_label(mongo_db) -> None:
    _seed(mongo_db)
    # Auto-apply via high confidence dispatch
    result = dispatch(
        ProposeParent(
            child_label="MPL-002", parent_label="MPL-001",
            reason="x", confidence=9.5,
        ),
        mongo_db,
    )
    assert result.status == "applied"
    entry = OntologyEntryRepository(mongo_db).get("MPL-002")
    assert entry.parent_label == "MPL-001"

    rolled = rollback_proposal(result.proposal_id, mongo_db, note="redo")
    assert rolled.new_status == "rolled_back"

    # Edge gone, parent_label None
    entry = OntologyEntryRepository(mongo_db).get("MPL-002")
    assert entry.parent_label is None
    assert OntologyTreeRepository(mongo_db).children_of("MPL-001") == []

    # Audit chain preserves both apply + rollback details
    updated = get_proposal(result.proposal_id, mongo_db)
    assert updated.status == "rolled_back"
    assert updated.application_result["rollback_result"]["parent_label_cleared"] is True
    assert updated.operator_note == "redo"


def test_rollback_reparenting_restores_previous_parent(mongo_db) -> None:
    _seed(mongo_db)
    # Initial parent: MPL-002 -> MPL-003.
    OntologyTreeRepository(mongo_db).add_edge(OntologyTreeEdge(
        parent_label="MPL-003", child_label="MPL-002",
    ))
    mongo_db.ontology_entries.update_one(
        {"_id": "MPL-002"}, {"$set": {"parent_label": "MPL-003"}}
    )
    # Re-parent at high confidence.
    result = dispatch(
        ProposeParent(
            child_label="MPL-002", parent_label="MPL-001",
            reason="x", confidence=9.0,
        ),
        mongo_db,
    )
    assert result.status == "applied"
    entry = OntologyEntryRepository(mongo_db).get("MPL-002")
    assert entry.parent_label == "MPL-001"

    # Roll back the re-parenting.
    rollback_proposal(result.proposal_id, mongo_db)

    # Old parent restored, new edge gone.
    entry = OntologyEntryRepository(mongo_db).get("MPL-002")
    assert entry.parent_label == "MPL-003"
    assert OntologyTreeRepository(mongo_db).children_of("MPL-003") == ["MPL-002"]
    assert OntologyTreeRepository(mongo_db).children_of("MPL-001") == []


def test_rollback_alias_pulls_value(mongo_db) -> None:
    _seed(mongo_db)
    result = dispatch(
        ProposeAlias(
            label="MPL-001", alias="bedrock",
            reason="synonym", confidence=9.0,
        ),
        mongo_db,
    )
    assert result.status == "applied"
    entry = OntologyEntryRepository(mongo_db).get("MPL-001")
    assert "bedrock" in entry.aliases

    rollback_proposal(result.proposal_id, mongo_db)
    entry = OntologyEntryRepository(mongo_db).get("MPL-001")
    assert "bedrock" not in entry.aliases


def test_rollback_rejects_non_applied(mongo_db) -> None:
    _seed(mongo_db)
    proposal = _pending_parent_proposal(mongo_db, "MPL-002", "MPL-001")
    with pytest.raises(ProposalError):
        rollback_proposal(proposal.proposal_id, mongo_db)


def test_reject_records_decided_via(mongo_db) -> None:
    from mahalath.actions import ProposeParent, dispatch
    from mahalath.db.models import OntologyEntry
    from mahalath.db.repositories import (
        ActionProposalRepository,
        OntologyEntryRepository,
    )
    from mahalath.proposals import reject_proposal

    repo = OntologyEntryRepository(mongo_db)
    repo.insert(OntologyEntry(mpl_label="MPL-001", canonical_term="alpha",
                              confidence=8.0))
    repo.insert(OntologyEntry(mpl_label="MPL-002", canonical_term="beta",
                              confidence=8.0))
    # Confidence below the reparent threshold lands in pending_review.
    outcome = dispatch(
        ProposeParent(child_label="MPL-002", parent_label="MPL-001",
                      reason="t", confidence=5.0),
        mongo_db,
    )
    assert outcome.status == "pending_review"
    reject_proposal(outcome.proposal_id, mongo_db,
                    note="delegate verdict",
                    decided_via="claude_delegate")
    stored = ActionProposalRepository(mongo_db).get(outcome.proposal_id)
    assert stored.operator_decision == "rejected"
    assert stored.decided_via == "claude_delegate"
