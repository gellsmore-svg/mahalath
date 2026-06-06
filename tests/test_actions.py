"""Action types + dispatcher tests against a live MongoDB test db.

Covers: parsing JSON action arrays, validation (existence, cycle,
duplicate alias, variant-as-parent block), auto-apply at threshold,
destructive routing to pending_review, invalid status with rationale.
"""

from __future__ import annotations

import pytest

from mahalath.actions import (
    DispatchResult,
    ProposeAlias,
    ProposeMerge,
    ProposeParent,
    ProposeSplit,
    apply,
    dispatch,
    parse_actions,
    validate,
)
from mahalath.db.models import (
    DefinitionVersion,
    OntologyEntry,
    OntologyTreeEdge,
)
from mahalath.db.repositories import (
    ActionProposalRepository,
    OntologyEntryRepository,
    OntologyTreeRepository,
)


def _seed(mongo_db) -> None:
    """Populate three top-level entries used by most tests."""
    repo = OntologyEntryRepository(mongo_db)
    repo.insert(OntologyEntry(
        mpl_label="MPL-001", canonical_term="Substrate",
        confidence=8.0,
        definitions=[DefinitionVersion(text="The underlying medium.")],
    ))
    repo.insert(OntologyEntry(
        mpl_label="MPL-002", canonical_term="Relational Substrate",
        confidence=8.5,
        definitions=[DefinitionVersion(text="The relational variant.")],
    ))
    repo.insert(OntologyEntry(
        mpl_label="MPL-003", canonical_term="Continuity",
        confidence=8.0,
        definitions=[DefinitionVersion(text="No gaps.")],
    ))


# --- Parsing ----------------------------------------------------------------


def test_parse_actions_happy_path() -> None:
    response = {
        "actions": [
            {
                "type": "propose_parent",
                "child_label": "MPL-002",
                "parent_label": "MPL-001",
                "reason": "RS is a kind of substrate",
                "confidence": 9.0,
            },
            {
                "type": "propose_alias",
                "label": "MPL-001",
                "alias": "underlying medium",
                "reason": "common synonym",
                "confidence": 8.2,
            },
        ]
    }
    actions = parse_actions(response, proposed_by="hierarchy_review")
    assert len(actions) == 2
    a, b = actions
    assert isinstance(a, ProposeParent)
    assert a.child_label == "MPL-002"
    assert a.parent_label == "MPL-001"
    assert a.confidence == 9.0
    assert a.proposed_by == "hierarchy_review"
    assert isinstance(b, ProposeAlias)
    assert b.label == "MPL-001"
    assert b.alias == "underlying medium"


def test_parse_actions_skips_unknown_type() -> None:
    response = {
        "actions": [
            {"type": "propose_marriage", "reason": "...", "confidence": 9.0},
            {"type": "propose_alias", "label": "MPL-001", "alias": "x",
             "reason": "y", "confidence": 8.0},
        ]
    }
    actions = parse_actions(response)
    assert len(actions) == 1
    assert isinstance(actions[0], ProposeAlias)


def test_parse_actions_returns_empty_when_missing() -> None:
    assert parse_actions({}) == []
    assert parse_actions({"actions": "not a list"}) == []
    assert parse_actions({"actions": []}) == []


def test_parse_actions_clamps_confidence() -> None:
    response = {
        "actions": [
            {"type": "propose_alias", "label": "MPL-001", "alias": "x",
             "reason": "y", "confidence": 15.0},
        ]
    }
    [a] = parse_actions(response)
    assert a.confidence == 10.0


def test_parse_actions_split_requires_list_in_into() -> None:
    response = {
        "actions": [
            {"type": "propose_split", "label": "MPL-001", "into": "not a list",
             "reason": "y", "confidence": 9.0},
        ]
    }
    assert parse_actions(response) == []


# --- Validation -------------------------------------------------------------


def test_validate_parent_happy_path(mongo_db) -> None:
    _seed(mongo_db)
    action = ProposeParent(
        child_label="MPL-002", parent_label="MPL-001",
        reason="rs is a substrate", confidence=9.0,
    )
    result = validate(action, mongo_db)
    assert result.valid


def test_validate_parent_rejects_self_loop(mongo_db) -> None:
    _seed(mongo_db)
    action = ProposeParent(
        child_label="MPL-001", parent_label="MPL-001",
        reason="x", confidence=9.0,
    )
    result = validate(action, mongo_db)
    assert not result.valid
    assert "same label" in result.reason


def test_validate_parent_rejects_missing_label(mongo_db) -> None:
    _seed(mongo_db)
    action = ProposeParent(
        child_label="MPL-002", parent_label="MPL-999",
        reason="x", confidence=9.0,
    )
    result = validate(action, mongo_db)
    assert not result.valid
    assert "does not exist" in result.reason


def test_validate_parent_rejects_already_parented(mongo_db) -> None:
    _seed(mongo_db)
    # Manually set MPL-002 as already having a parent.
    mongo_db.ontology_entries.update_one(
        {"_id": "MPL-002"}, {"$set": {"parent_label": "MPL-003"}}
    )
    action = ProposeParent(
        child_label="MPL-002", parent_label="MPL-001",
        reason="x", confidence=9.0,
    )
    result = validate(action, mongo_db)
    assert not result.valid
    assert "already has parent" in result.reason


def test_validate_parent_detects_cycle(mongo_db) -> None:
    _seed(mongo_db)
    # Existing edge: MPL-001 -> MPL-002
    OntologyTreeRepository(mongo_db).add_edge(OntologyTreeEdge(
        parent_label="MPL-001", child_label="MPL-002",
    ))
    mongo_db.ontology_entries.update_one(
        {"_id": "MPL-002"}, {"$set": {"parent_label": "MPL-001"}}
    )
    # Now try MPL-002 -> MPL-001 (would cycle).
    # MPL-001 isn't currently parented, so the "already parented" guard doesn't block.
    action = ProposeParent(
        child_label="MPL-001", parent_label="MPL-002",
        reason="x", confidence=9.0,
    )
    result = validate(action, mongo_db)
    assert not result.valid
    assert "cycle" in result.reason


def test_validate_alias_happy_path(mongo_db) -> None:
    _seed(mongo_db)
    action = ProposeAlias(
        label="MPL-001", alias="underlying medium",
        reason="synonym", confidence=8.5,
    )
    assert validate(action, mongo_db).valid


def test_validate_alias_rejects_canonical_collision(mongo_db) -> None:
    _seed(mongo_db)
    action = ProposeAlias(
        label="MPL-001", alias="Continuity",
        reason="x", confidence=9.0,
    )
    result = validate(action, mongo_db)
    assert not result.valid
    assert "MPL-003" in result.reason


def test_validate_alias_rejects_empty(mongo_db) -> None:
    _seed(mongo_db)
    action = ProposeAlias(
        label="MPL-001", alias="   ",
        reason="x", confidence=9.0,
    )
    assert not validate(action, mongo_db).valid


# --- Dispatch ---------------------------------------------------------------


def test_dispatch_parent_above_threshold_applies(mongo_db) -> None:
    _seed(mongo_db)
    action = ProposeParent(
        child_label="MPL-002", parent_label="MPL-001",
        reason="rs is a substrate", confidence=9.0,
        proposed_by="hierarchy_review",
        source_decision_log_id="dl-abc",
    )
    result = dispatch(action, mongo_db, auto_apply_threshold=8.0)
    assert isinstance(result, DispatchResult)
    assert result.status == "applied"

    # Verify on disk
    entry = OntologyEntryRepository(mongo_db).get("MPL-002")
    assert entry.parent_label == "MPL-001"
    children = OntologyTreeRepository(mongo_db).children_of("MPL-001")
    assert children == ["MPL-002"]

    # Audit row written with full provenance
    proposals = ActionProposalRepository(mongo_db).by_status("applied")
    assert len(proposals) == 1
    p = proposals[0]
    assert p.action_type == "propose_parent"
    assert p.confidence == 9.0
    assert p.proposed_by == "hierarchy_review"
    assert p.source_decision_log_id == "dl-abc"
    assert p.applied_at is not None
    assert p.application_result["tree_edge_added"] is True


def test_dispatch_alias_above_threshold_applies(mongo_db) -> None:
    _seed(mongo_db)
    action = ProposeAlias(
        label="MPL-001", alias="underlying medium",
        reason="synonym", confidence=8.5,
    )
    result = dispatch(action, mongo_db)
    assert result.status == "applied"
    entry = OntologyEntryRepository(mongo_db).get("MPL-001")
    assert "underlying medium" in entry.aliases


def test_dispatch_below_threshold_routes_to_review(mongo_db) -> None:
    _seed(mongo_db)
    action = ProposeAlias(
        label="MPL-001", alias="something tentative",
        reason="x", confidence=6.0,
    )
    result = dispatch(action, mongo_db, auto_apply_threshold=8.0)
    assert result.status == "pending_review"
    # Side effect was NOT applied.
    entry = OntologyEntryRepository(mongo_db).get("MPL-001")
    assert "something tentative" not in entry.aliases
    # But the proposal is recorded.
    assert ActionProposalRepository(mongo_db).by_status("pending_review")


def test_dispatch_invalid_action_records_proposal_as_invalid(mongo_db) -> None:
    _seed(mongo_db)
    action = ProposeParent(
        child_label="MPL-002", parent_label="MPL-NOPE",
        reason="x", confidence=9.5,
    )
    result = dispatch(action, mongo_db)
    assert result.status == "invalid"
    assert "does not exist" in result.detail
    [proposal] = ActionProposalRepository(mongo_db).by_status("invalid")
    assert proposal.rejection_reason is not None


def test_dispatch_merge_routes_to_review_even_at_high_confidence(mongo_db) -> None:
    _seed(mongo_db)
    action = ProposeMerge(
        keep_label="MPL-001", drop_label="MPL-002",
        reason="duplicates", confidence=9.8,
    )
    result = dispatch(action, mongo_db)
    assert result.status == "pending_review"
    # Drop label still active — destructive action did not auto-apply.
    assert OntologyEntryRepository(mongo_db).get("MPL-002") is not None


def test_apply_destructive_action_raises_not_implemented(mongo_db) -> None:
    _seed(mongo_db)
    action = ProposeSplit(
        label="MPL-001", into=("Soft Substrate", "Hard Substrate"),
        reason="x", confidence=9.0,
    )
    with pytest.raises(NotImplementedError):
        apply(action, mongo_db)
