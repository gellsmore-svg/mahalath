"""Frontier-review tests with MockAdapter returning canned verdicts."""

from __future__ import annotations

import json

import pytest

from mahalath.actions import ProposeParent, dispatch
from mahalath.adapters import MockAdapter
from mahalath.db.models import DefinitionVersion, OntologyEntry
from mahalath.db.repositories import (
    ActionProposalRepository,
    OntologyEntryRepository,
)
from mahalath.frontier import (
    FrontierReviewError,
    build_review_prompt,
    frontier_review,
    parse_verdict,
)


def _verdict(decision: str, confidence: float, reasoning: str) -> str:
    return json.dumps({
        "decision": decision,
        "confidence": confidence,
        "reasoning": reasoning,
    })


def _seed(mongo_db) -> None:
    repo = OntologyEntryRepository(mongo_db)
    repo.insert(OntologyEntry(
        mpl_label="MPL-001", canonical_term="Substrate", confidence=8.0,
        definitions=[DefinitionVersion(text="The fundamental underlying medium.", model_used="gemma4:e2b")],
    ))
    repo.insert(OntologyEntry(
        mpl_label="MPL-002", canonical_term="Relational Substrate", confidence=8.5,
        definitions=[DefinitionVersion(text="The relational variant of the substrate.", model_used="gemma4:e2b")],
    ))


def _pending(mongo_db, *, child: str, parent: str, conf: float = 7.0):
    dr = dispatch(
        ProposeParent(
            child_label=child, parent_label=parent,
            reason="test", confidence=conf,
        ),
        mongo_db,
        auto_apply_threshold=9.0,  # force pending_review
    )
    return ActionProposalRepository(mongo_db).get(dr.proposal_id)


# --- parse_verdict --------------------------------------------------------


def test_parse_verdict_happy_path() -> None:
    v = parse_verdict(_verdict("accept", 8.5, "edge is correct"))
    assert v.decision == "accept"
    assert v.confidence == 8.5
    assert v.reasoning == "edge is correct"


def test_parse_verdict_tolerates_fenced_json() -> None:
    raw = "```json\n" + _verdict("reject", 7.0, "backwards") + "\n```"
    v = parse_verdict(raw)
    assert v.decision == "reject"
    assert v.confidence == 7.0


def test_parse_verdict_clamps_confidence() -> None:
    assert parse_verdict(_verdict("accept", 15.0, "x")).confidence == 10.0
    assert parse_verdict(_verdict("reject", -1.0, "x")).confidence == 0.0


def test_parse_verdict_rejects_invalid_decision() -> None:
    with pytest.raises(FrontierReviewError):
        parse_verdict(_verdict("maybe", 8.0, "..."))


def test_parse_verdict_rejects_empty() -> None:
    with pytest.raises(FrontierReviewError):
        parse_verdict("")


def test_parse_verdict_finds_object_in_prose() -> None:
    raw = (
        "Sure, here is my verdict: "
        + _verdict("escalate", 5.0, "ambiguous")
        + " hope that helps."
    )
    v = parse_verdict(raw)
    assert v.decision == "escalate"


# --- build_review_prompt --------------------------------------------------


def test_build_review_prompt_includes_definitions(mongo_db) -> None:
    _seed(mongo_db)
    proposal = _pending(mongo_db, child="MPL-002", parent="MPL-001")
    prompt = build_review_prompt(proposal, mongo_db, style_overlay=None)
    assert "MPL-001" in prompt
    assert "MPL-002" in prompt
    assert "Substrate" in prompt
    assert "Relational Substrate" in prompt
    assert "fundamental underlying medium" in prompt
    assert "relational variant" in prompt


def test_build_review_prompt_includes_style_overlay(mongo_db) -> None:
    _seed(mongo_db)
    proposal = _pending(mongo_db, child="MPL-002", parent="MPL-001")
    prompt = build_review_prompt(
        proposal, mongo_db,
        style_overlay="Use declarative ontology; no hedging.",
    )
    assert "declarative ontology" in prompt


# --- frontier_review (end-to-end with MockAdapter) ------------------------


def test_frontier_review_accept_routes_through_accept_proposal(mongo_db, mongo_config) -> None:
    config = mongo_config
    _seed(mongo_db)
    proposal = _pending(mongo_db, child="MPL-002", parent="MPL-001", conf=7.0)

    adapter = MockAdapter(default_response=_verdict("accept", 9.0, "looks right"))
    result = frontier_review(config, mongo_db, adapter, max_items=10)

    assert result.items_reviewed == 1
    assert result.items_accepted == 1
    assert result.items_rejected == 0

    # Side effect: ontology edge applied
    entry = OntologyEntryRepository(mongo_db).get("MPL-002")
    assert entry.parent_label == "MPL-001"

    # Audit: operator_note carries the frontier reasoning
    stored = ActionProposalRepository(mongo_db).get(proposal.proposal_id)
    assert stored.status == "applied"
    assert "frontier-review" in (stored.operator_note or "")
    assert "looks right" in (stored.operator_note or "")


def test_frontier_review_reject_routes_through_reject_proposal(mongo_db, mongo_config) -> None:
    config = mongo_config
    _seed(mongo_db)
    proposal = _pending(mongo_db, child="MPL-002", parent="MPL-001", conf=7.0)

    adapter = MockAdapter(default_response=_verdict("reject", 9.0, "backwards"))
    result = frontier_review(config, mongo_db, adapter)

    assert result.items_rejected == 1
    entry = OntologyEntryRepository(mongo_db).get("MPL-002")
    assert entry.parent_label is None  # nothing applied

    stored = ActionProposalRepository(mongo_db).get(proposal.proposal_id)
    assert stored.status == "rejected"
    assert "backwards" in (stored.operator_note or "")


def test_frontier_review_escalate_leaves_pending(mongo_db, mongo_config) -> None:
    config = mongo_config
    _seed(mongo_db)
    proposal = _pending(mongo_db, child="MPL-002", parent="MPL-001")

    adapter = MockAdapter(default_response=_verdict("escalate", 5.0, "needs human judgment"))
    result = frontier_review(config, mongo_db, adapter)

    assert result.items_escalated == 1
    stored = ActionProposalRepository(mongo_db).get(proposal.proposal_id)
    assert stored.status == "pending_review"  # unchanged
    assert "frontier-escalated" in (stored.operator_note or "")
    assert "needs human judgment" in (stored.operator_note or "")


def test_frontier_review_caps_at_max_items(mongo_db, mongo_config) -> None:
    config = mongo_config
    _seed(mongo_db)
    # Create three more entries to host three pending parent proposals.
    repo = OntologyEntryRepository(mongo_db)
    for i in range(3, 6):
        repo.insert(OntologyEntry(
            mpl_label=f"MPL-00{i}", canonical_term=f"term-{i}", confidence=8.0,
        ))
    for i in range(3, 6):
        _pending(mongo_db, child=f"MPL-00{i}", parent="MPL-001")

    adapter = MockAdapter(default_response=_verdict("escalate", 5.0, "x"))
    result = frontier_review(config, mongo_db, adapter, max_items=2)
    assert result.items_reviewed == 2
    # Three pending still — 3 created, 2 reviewed (still pending after escalate),
    # so all 3 remain in pending_review.
    pending = ActionProposalRepository(mongo_db).by_status("pending_review")
    assert len(pending) == 3
