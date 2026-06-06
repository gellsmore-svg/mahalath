"""Hierarchy review tests against a live MongoDB test db.

Covers: prompt construction, snapshot exclusion of the focus entry,
adapter integration through MockAdapter, OntologyReview audit row
written, action parsing carries source_ontology_review_id.
"""

from __future__ import annotations

import json

import pytest

from mahalath.actions import ProposeParent
from mahalath.adapters import MockAdapter
from mahalath.config import RuntimeConfig
from mahalath.db.models import DefinitionVersion, OntologyEntry
from mahalath.db.repositories import (
    OntologyEntryRepository,
    OntologyReviewRepository,
)
from mahalath.hierarchy import (
    HierarchyReviewError,
    build_review_prompt,
    run_hierarchy_review,
)


def _seed(mongo_db) -> None:
    repo = OntologyEntryRepository(mongo_db)
    repo.insert(OntologyEntry(
        mpl_label="MPL-001", canonical_term="Substrate", confidence=8.0,
        definitions=[DefinitionVersion(text="The fundamental underlying medium.")],
    ))
    repo.insert(OntologyEntry(
        mpl_label="MPL-002", canonical_term="Relational Substrate", confidence=8.5,
        definitions=[DefinitionVersion(text="The relational variant of substrate.")],
    ))


def _review_response(actions: list[dict], no_actions_reason: str | None = None) -> str:
    return json.dumps({"actions": actions, "no_actions_reason": no_actions_reason})


def test_build_review_prompt_lists_existing_and_focus(mongo_db) -> None:
    _seed(mongo_db)
    entries = OntologyEntryRepository(mongo_db)
    focus = entries.get("MPL-002")
    snapshot = [entries.get("MPL-001")]
    prompt = build_review_prompt(snapshot, focus)
    assert "MPL-001: Substrate" in prompt
    assert "MPL-002: Relational Substrate" in prompt
    assert "NEW entry being reviewed:" in prompt
    assert "Existing ontology entries:" in prompt
    assert "propose_parent" in prompt


def test_run_hierarchy_review_returns_parsed_actions(mongo_db) -> None:
    _seed(mongo_db)
    raw = _review_response([
        {
            "type": "propose_parent",
            "child_label": "MPL-002",
            "parent_label": "MPL-001",
            "reason": "RS is a kind of substrate",
            "confidence": 9.0,
        }
    ])
    adapter = MockAdapter(responses={"ontology curator": raw})
    runtime = RuntimeConfig()

    result = run_hierarchy_review(
        "MPL-002", mongo_db, adapter, runtime,
        source_decision_log_id="dl-x",
    )
    assert len(result.actions) == 1
    action = result.actions[0]
    assert isinstance(action, ProposeParent)
    assert action.child_label == "MPL-002"
    assert action.parent_label == "MPL-001"
    assert action.source_decision_log_id == "dl-x"
    assert action.source_ontology_review_id == result.review_id
    assert action.proposed_by == "hierarchy_review"


def test_run_hierarchy_review_persists_review_record(mongo_db) -> None:
    _seed(mongo_db)
    raw = _review_response([{
        "type": "propose_parent",
        "child_label": "MPL-002",
        "parent_label": "MPL-001",
        "reason": "...", "confidence": 9.0,
    }])
    adapter = MockAdapter(responses={"ontology curator": raw})
    runtime = RuntimeConfig()
    result = run_hierarchy_review("MPL-002", mongo_db, adapter, runtime)

    stored = OntologyReviewRepository(mongo_db).get(result.review_id)
    assert stored is not None
    assert stored.focus_mpl_label == "MPL-002"
    assert stored.actions_count == 1
    assert stored.triggered_by == "post_accept"
    assert stored.prompt
    assert stored.response


def test_run_hierarchy_review_no_actions_branch(mongo_db) -> None:
    _seed(mongo_db)
    raw = _review_response([], no_actions_reason="No clear relationship.")
    adapter = MockAdapter(responses={"ontology curator": raw})
    runtime = RuntimeConfig()
    result = run_hierarchy_review("MPL-002", mongo_db, adapter, runtime)
    assert result.actions == []
    assert result.no_actions_reason == "No clear relationship."

    stored = OntologyReviewRepository(mongo_db).get(result.review_id)
    assert stored.actions_count == 0
    assert stored.no_actions_reason == "No clear relationship."


def test_run_hierarchy_review_tolerates_prose_preamble(mongo_db) -> None:
    _seed(mongo_db)
    raw = (
        "Sure, here is the JSON you asked for:\n"
        + _review_response([])
        + "\nHope that helps."
    )
    adapter = MockAdapter(responses={"ontology curator": raw})
    runtime = RuntimeConfig()
    result = run_hierarchy_review("MPL-002", mongo_db, adapter, runtime)
    # Should still parse out the JSON object.
    assert result.actions == []


def test_run_hierarchy_review_excludes_focus_from_snapshot(mongo_db) -> None:
    _seed(mongo_db)
    raw = _review_response([])
    adapter = MockAdapter(responses={"ontology curator": raw})
    runtime = RuntimeConfig()
    run_hierarchy_review("MPL-002", mongo_db, adapter, runtime)
    # The prompt sent to the adapter should NOT contain MPL-002 in the
    # "Existing ontology entries" snapshot above the focus.
    prompt = adapter.calls[0]["prompt"]
    snapshot_section = prompt.split("NEW entry being reviewed:")[0]
    assert "MPL-001: Substrate" in snapshot_section
    assert "MPL-002:" not in snapshot_section


def test_run_hierarchy_review_raises_when_focus_missing(mongo_db) -> None:
    _seed(mongo_db)
    adapter = MockAdapter(responses={"ontology curator": _review_response([])})
    runtime = RuntimeConfig()
    with pytest.raises(HierarchyReviewError):
        run_hierarchy_review("MPL-999", mongo_db, adapter, runtime)


def test_run_hierarchy_review_handles_empty_ontology(mongo_db) -> None:
    """First-entry review: snapshot is empty but the call still works."""
    repo = OntologyEntryRepository(mongo_db)
    repo.insert(OntologyEntry(
        mpl_label="MPL-001", canonical_term="Alone", confidence=8.0,
        definitions=[DefinitionVersion(text="Only entry.")],
    ))
    raw = _review_response([], no_actions_reason="Nothing to compare against.")
    adapter = MockAdapter(responses={"ontology curator": raw})
    runtime = RuntimeConfig()
    result = run_hierarchy_review("MPL-001", mongo_db, adapter, runtime)
    assert result.actions == []
    assert result.no_actions_reason == "Nothing to compare against."
    # Prompt should indicate empty snapshot.
    prompt = adapter.calls[0]["prompt"]
    assert "(none" in prompt