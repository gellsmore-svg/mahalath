"""Hierarchy review tests against a live MongoDB test db.

Covers: prompt construction, snapshot exclusion of the focus entry,
adapter integration through MockAdapter, OntologyReview audit row
written, action parsing carries source_ontology_review_id.
"""

from __future__ import annotations

import json

import pytest

from dataclasses import dataclass, field

from mahalath.actions import ProposeAlias, ProposeParent
from mahalath.adapters import MockAdapter
from mahalath.adapters.base import AdapterResponse
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
    run_hierarchy_review_consensus,
)


@dataclass
class SequentialMockAdapter:
    """MockAdapter that returns a different canned response per call.

    Cycles back to the start if exhausted, so a single-cycle response
    list is enough for most tests. Useful for exercising consensus
    aggregation across passes that disagree.
    """

    responses: list[str]
    name: str = "sequential_mock"
    default_model: str = "mock-model"
    calls: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self._index = 0

    def generate(
        self, prompt, *, model=None, timeout_seconds=None, want_json=False
    ):
        self.calls.append({
            "prompt": prompt, "model": model,
            "timeout_seconds": timeout_seconds, "want_json": want_json,
        })
        text = self.responses[self._index % len(self.responses)]
        self._index += 1
        return AdapterResponse(
            text=text, model=model or self.default_model, duration_ms=0
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


def test_consensus_passes_through_when_all_agree(mongo_db) -> None:
    _seed(mongo_db)
    response = _review_response([{
        "type": "propose_parent",
        "child_label": "MPL-002", "parent_label": "MPL-001",
        "reason": "RS specialises Substrate", "confidence": 8.7,
    }])
    adapter = SequentialMockAdapter(responses=[response, response, response])
    runtime = RuntimeConfig()
    result = run_hierarchy_review_consensus(
        "MPL-002", mongo_db, adapter, runtime, n_passes=3
    )
    assert result.n_passes == 3
    assert len(result.review_ids) == 3
    assert len(result.consensus_actions) == 1
    [action] = result.consensus_actions
    assert isinstance(action, ProposeParent)
    assert action.parent_label == "MPL-001"
    assert action.confidence == 8.7  # min of three 8.7s


def test_consensus_takes_minimum_confidence(mongo_db) -> None:
    _seed(mongo_db)
    base = {
        "type": "propose_parent",
        "child_label": "MPL-002", "parent_label": "MPL-001",
        "reason": "x",
    }
    adapter = SequentialMockAdapter(responses=[
        _review_response([{**base, "confidence": 9.5}]),
        _review_response([{**base, "confidence": 8.2}]),
        _review_response([{**base, "confidence": 9.0}]),
    ])
    runtime = RuntimeConfig()
    result = run_hierarchy_review_consensus(
        "MPL-002", mongo_db, adapter, runtime, n_passes=3
    )
    assert len(result.consensus_actions) == 1
    assert result.consensus_actions[0].confidence == 8.2  # min of 9.5, 8.2, 9.0


def test_consensus_drops_action_when_one_pass_disagrees(mongo_db) -> None:
    _seed(mongo_db)
    adapter = SequentialMockAdapter(responses=[
        _review_response([{
            "type": "propose_parent",
            "child_label": "MPL-002", "parent_label": "MPL-001",
            "reason": "x", "confidence": 9.0,
        }]),
        _review_response([{
            "type": "propose_parent",
            "child_label": "MPL-002", "parent_label": "MPL-001",
            "reason": "x", "confidence": 8.5,
        }]),
        # Pass 3: silence — no action proposed.
        _review_response([]),
    ])
    runtime = RuntimeConfig()
    result = run_hierarchy_review_consensus(
        "MPL-002", mongo_db, adapter, runtime, n_passes=3
    )
    # 2/3 is not unanimous; consensus rejects.
    assert result.consensus_actions == []


def test_consensus_treats_direction_flip_as_disagreement(mongo_db) -> None:
    """Direction-variance is the headline use case: model proposes
    X→child of Y in one pass and Y→child of X in another. Strict
    unanimity must reject both."""
    _seed(mongo_db)
    adapter = SequentialMockAdapter(responses=[
        _review_response([{
            "type": "propose_parent",
            "child_label": "MPL-002", "parent_label": "MPL-001",
            "reason": "x", "confidence": 8.5,
        }]),
        _review_response([{
            "type": "propose_parent",
            "child_label": "MPL-001", "parent_label": "MPL-002",
            "reason": "x", "confidence": 8.5,
        }]),
        _review_response([{
            "type": "propose_parent",
            "child_label": "MPL-002", "parent_label": "MPL-001",
            "reason": "x", "confidence": 8.5,
        }]),
    ])
    runtime = RuntimeConfig()
    result = run_hierarchy_review_consensus(
        "MPL-002", mongo_db, adapter, runtime, n_passes=3
    )
    # Direction A: 2 passes. Direction B: 1 pass. Neither is unanimous.
    assert result.consensus_actions == []
    # But all three pass results are preserved for diagnostics.
    assert len(result.per_pass_actions) == 3


def test_consensus_n_passes_one_is_passthrough(mongo_db) -> None:
    _seed(mongo_db)
    adapter = SequentialMockAdapter(responses=[
        _review_response([{
            "type": "propose_parent",
            "child_label": "MPL-002", "parent_label": "MPL-001",
            "reason": "x", "confidence": 7.0,
        }])
    ])
    runtime = RuntimeConfig()
    result = run_hierarchy_review_consensus(
        "MPL-002", mongo_db, adapter, runtime, n_passes=1
    )
    # Single-pass mode: everything passes through, confidence preserved.
    assert len(result.consensus_actions) == 1
    assert result.consensus_actions[0].confidence == 7.0


def test_consensus_persists_one_review_record_per_pass(mongo_db) -> None:
    _seed(mongo_db)
    response = _review_response([])
    adapter = SequentialMockAdapter(responses=[response] * 3)
    runtime = RuntimeConfig()
    result = run_hierarchy_review_consensus(
        "MPL-002", mongo_db, adapter, runtime, n_passes=3
    )
    assert len(result.review_ids) == 3
    # Every review_id is queryable individually.
    review_repo = OntologyReviewRepository(mongo_db)
    for rid in result.review_ids:
        assert review_repo.get(rid) is not None


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

def test_snapshot_is_language_scoped(mongo_db) -> None:
    from mahalath.db.models import OntologyEntry
    from mahalath.db.repositories import OntologyEntryRepository
    from mahalath.hierarchy import _snapshot

    repo = OntologyEntryRepository(mongo_db)
    repo.insert(OntologyEntry(mpl_label="MPL-001", canonical_term="weave",
                              confidence=8.0, language="en"))
    repo.insert(OntologyEntry(mpl_label="MPL-002", canonical_term="Gewebe",
                              confidence=8.0, language="de"))
    repo.insert(OntologyEntry(mpl_label="MPL-003", canonical_term="Faser",
                              confidence=8.0, language="de"))

    labels = [e.mpl_label for e in _snapshot(repo, "MPL-003", limit=50)]
    assert labels == ["MPL-002"]  # same-lexicon only, focus excluded


def test_consensus_passes_rotate_model_roster(mongo_db) -> None:
    import json
    from mahalath.adapters import MockAdapter
    from mahalath.config import RuntimeConfig
    from mahalath.db.models import OntologyEntry
    from mahalath.db.repositories import OntologyEntryRepository
    from mahalath.hierarchy import run_hierarchy_review_consensus

    OntologyEntryRepository(mongo_db).insert(OntologyEntry(
        mpl_label="MPL-001", canonical_term="alpha", confidence=8.0,
    ))
    adapter = MockAdapter(default_response=json.dumps(
        {"actions": [], "no_actions_reason": "nothing to do"}))
    runtime = RuntimeConfig(
        consensus_models=["family-a", "family-b", "family-c"],
    )
    run_hierarchy_review_consensus(
        "MPL-001", mongo_db, adapter, runtime, n_passes=3,
    )
    assert [c["model"] for c in adapter.calls] == [
        "family-a", "family-b", "family-c",
    ]
