"""Cross-language mapping machinery tests (M-C, ADR-029).

Covers: relation-taxonomy seeding; the majority attribution gate
(operator-ruled 2026-06-12: majority verdict decides, confidence =
median across typed votes — majority-type accepted / majority-none
rejected / three-way-split unresolved / under-threshold unresolved /
roster rotation / scale correction); illocution comparison;
generation dry-run vs apply incl. invented-label fail-closed and
existing-pair skip; staleness participation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field as _field

from mahalath.adapters.base import AdapterResponse
from mahalath.db.models import DefinitionVersion, OntologyEntry
from mahalath.db.repositories import (
    MappingRepository,
    OntologyEntryRepository,
)
from mahalath.mappings import (
    ATTRIBUTION_TAG,
    CANDIDATE_TAG,
    attribute_mapping,
    compare_illocution,
    generate_mappings,
    mark_mappings_stale,
    seed_mapping_relations,
)


@dataclass
class _SeqAdapter:
    responses: list[str]
    name: str = "sequential_mock"
    default_model: str = "mock-model"
    calls: list = _field(default_factory=list)

    def __post_init__(self) -> None:
        self._index = 0

    def generate(self, prompt, *, model=None, timeout_seconds=None,
                 want_json=False):
        self.calls.append({"prompt": prompt, "model": model})
        text = self.responses[self._index % len(self.responses)]
        self._index += 1
        return AdapterResponse(
            text=text, model=model or self.default_model, duration_ms=0)


def _verdict(relationship: str, confidence: float = 9.0,
             rationale: str = "ok") -> str:
    return json.dumps({"relationship": relationship,
                       "confidence": confidence, "rationale": rationale})


def _seed_pair(mongo_db) -> None:
    repo = OntologyEntryRepository(mongo_db)
    repo.insert(OntologyEntry(
        mpl_label="MPL-038", canonical_term="coupling", confidence=8.0,
        language="en",
        definitions=[DefinitionVersion(
            text="The measure of constraint between configurations.")],
    ))
    repo.insert(OntologyEntry(
        mpl_label="MPL-117", canonical_term="Kopplung", confidence=9.0,
        language="de",
        definitions=[DefinitionVersion(
            text="Die Verbindung zweier schwingungsfähiger Systeme.",
            language="de")],
    ))


def test_seed_relations_idempotent(mongo_db) -> None:
    first = seed_mapping_relations(mongo_db)
    assert len(first["inserted"]) == 4
    second = seed_mapping_relations(mongo_db)
    assert second["inserted"] == []
    assert len(second["skipped_existing"]) == 4


def test_unanimous_relationship_accepted_and_stored(mongo_db) -> None:
    seed_mapping_relations(mongo_db)
    _seed_pair(mongo_db)
    adapter = _SeqAdapter(responses=[
        _verdict("partial_overlap", 9.0),
        _verdict("partial_overlap", 8.5),
        _verdict("partial_overlap", 8.2),
    ])
    a = attribute_mapping(
        mongo_db, "MPL-117", "MPL-038", adapter,
        passes=3, models=["fam-a", "fam-b", "fam-c"], apply=True,
    )
    assert a.status == "accepted"
    assert a.relationship == "partial_overlap"
    assert a.confidence == 8.5  # median across typed passes
    assert [c["model"] for c in adapter.calls] == ["fam-a", "fam-b", "fam-c"]

    stored = MappingRepository(mongo_db).get_pair("MPL-117", "MPL-038")
    assert stored is not None
    assert stored.status == "accepted"
    assert stored.source_language == "de" and stored.target_language == "en"
    assert stored.relationship_id is not None
    assert stored.models_used == ["fam-a", "fam-b", "fam-c"]


def test_unanimous_none_is_rejected(mongo_db) -> None:
    seed_mapping_relations(mongo_db)
    _seed_pair(mongo_db)
    adapter = _SeqAdapter(responses=[_verdict("none", 9.5)] * 3)
    a = attribute_mapping(mongo_db, "MPL-117", "MPL-038", adapter, passes=3)
    assert a.status == "rejected"
    assert a.relationship == "none"


def test_majority_type_accepted_over_dissenting_type(mongo_db) -> None:
    # The live dry-run shape: 2x partial_overlap vs 1x narrower_than,
    # with the majority's low scorer at 7.5 — median over the three
    # typed votes (8.5) clears the bar where min() (7.5) would not.
    seed_mapping_relations(mongo_db)
    _seed_pair(mongo_db)
    adapter = _SeqAdapter(responses=[
        _verdict("partial_overlap", 7.5),
        _verdict("partial_overlap", 8.5),
        _verdict("narrower_than", 8.5),
    ])
    a = attribute_mapping(mongo_db, "MPL-117", "MPL-038", adapter, passes=3)
    assert a.status == "accepted"
    assert a.relationship == "partial_overlap"
    assert a.confidence == 8.5
    assert len(a.per_pass) == 3  # the dissent stays on the record


def test_majority_none_is_rejected(mongo_db) -> None:
    # The scout-validates-its-own-candidates shape: one typed vote
    # against two none-votes is rejected, not parked as unresolved.
    seed_mapping_relations(mongo_db)
    _seed_pair(mongo_db)
    adapter = _SeqAdapter(responses=[
        _verdict("partial_overlap", 7.0),
        _verdict("none", 9.5),
        _verdict("none", 9.0),
    ])
    a = attribute_mapping(mongo_db, "MPL-117", "MPL-038", adapter, passes=3)
    assert a.status == "rejected"
    assert a.relationship == "none"
    assert a.confidence == 9.25  # median of the none votes


def test_three_way_split_is_unresolved(mongo_db) -> None:
    seed_mapping_relations(mongo_db)
    _seed_pair(mongo_db)
    adapter = _SeqAdapter(responses=[
        _verdict("equivalent", 9.0),
        _verdict("partial_overlap", 9.0),
        _verdict("narrower_than", 9.0),
    ])
    a = attribute_mapping(mongo_db, "MPL-117", "MPL-038", adapter, passes=3)
    assert a.status == "unresolved"
    assert a.relationship is None
    assert len(a.per_pass) == 3  # disagreement detail kept for review


def test_under_threshold_majority_is_unresolved(mongo_db) -> None:
    seed_mapping_relations(mongo_db)
    _seed_pair(mongo_db)
    adapter = _SeqAdapter(responses=[
        _verdict("equivalent", 9.0),
        _verdict("equivalent", 6.0),
        _verdict("equivalent", 7.5),   # median 7.5 under the 8.0 bar
    ])
    a = attribute_mapping(mongo_db, "MPL-117", "MPL-038", adapter, passes=3)
    assert a.status == "unresolved"
    assert a.relationship == "equivalent"
    assert a.confidence == 7.5


def test_fractional_confidence_rescaled(mongo_db) -> None:
    seed_mapping_relations(mongo_db)
    _seed_pair(mongo_db)
    adapter = _SeqAdapter(responses=[
        _verdict("equivalent", 0.9),   # the 0-1 scale quirk -> 9.0
        _verdict("equivalent", 8.5),
        _verdict("equivalent", 9.0),
    ])
    a = attribute_mapping(mongo_db, "MPL-117", "MPL-038", adapter, passes=3)
    assert a.status == "accepted"
    assert a.confidence == 9.0  # median(9.0, 8.5, 9.0)


def test_illocution_comparison(mongo_db) -> None:
    from mahalath.intents import resolve_intent_tag, seed_intents

    seed_intents(mongo_db)
    teach = resolve_intent_tag(mongo_db, "teach")
    warn = resolve_intent_tag(mongo_db, "warn")
    repo = OntologyEntryRepository(mongo_db)
    repo.insert(OntologyEntry(
        mpl_label="MPL-201", canonical_term="stewardship", confidence=8.0,
        language="en",
        definitions=[DefinitionVersion(text="x", intent_tags=[teach])],
    ))
    repo.insert(OntologyEntry(
        mpl_label="MPL-202", canonical_term="Verantwortung", confidence=8.0,
        language="de",
        definitions=[DefinitionVersion(text="y", intent_tags=[teach, warn])],
    ))
    cmp = compare_illocution(
        mongo_db,
        repo.get("MPL-201"), repo.get("MPL-202"),
    )
    assert cmp["shared"] == ["teach"]
    assert cmp["target_only"] == ["warn"]
    assert cmp["divergent"] is True


def test_generate_dry_run_stores_nothing(mongo_db, mongo_config) -> None:
    seed_mapping_relations(mongo_db)
    _seed_pair(mongo_db)
    adapter = _SeqAdapter(responses=[
        json.dumps({"candidates": ["MPL-038"]}),   # candidate pass
        _verdict("partial_overlap", 9.0),
        _verdict("partial_overlap", 9.0),
        _verdict("partial_overlap", 9.0),
    ])
    cfg = mongo_config
    result = generate_mappings(
        cfg, mongo_db, adapter,
        source_language="de", target_language="en", max_items=5,
    )
    assert result.candidate_pairs == 1
    assert result.accepted == 1
    assert mongo_db.mappings.count_documents({}) == 0  # dry-run


def test_generate_apply_fail_closed_and_skip_existing(mongo_db, mongo_config) -> None:
    seed_mapping_relations(mongo_db)
    _seed_pair(mongo_db)
    adapter = _SeqAdapter(responses=[
        # invented label is dropped; real one proceeds
        json.dumps({"candidates": ["MPL-999", "MPL-038"]}),
        _verdict("partial_overlap", 9.0),
        _verdict("partial_overlap", 9.0),
        _verdict("partial_overlap", 9.0),
    ])
    cfg = mongo_config
    result = generate_mappings(
        cfg, mongo_db, adapter,
        source_language="de", target_language="en", max_items=5, apply=True,
    )
    assert result.candidate_pairs == 1
    assert mongo_db.mappings.count_documents({}) == 1

    # Second run skips the already-mapped pair entirely.
    adapter2 = _SeqAdapter(responses=[
        json.dumps({"candidates": ["MPL-038"]}),
    ])
    result2 = generate_mappings(
        cfg, mongo_db, adapter2,
        source_language="de", target_language="en", max_items=5, apply=True,
    )
    assert result2.candidate_pairs == 0
    assert len(adapter2.calls) == 1  # only the candidate prompt ran


def test_resolve_mapping_records_operator_verdict(mongo_db) -> None:
    from mahalath.mappings import ResolutionError, resolve_mapping

    seed_mapping_relations(mongo_db)
    _seed_pair(mongo_db)
    # Land an unresolved mapping (three-way split, the Kopplung shape).
    adapter = _SeqAdapter(responses=[
        _verdict("partial_overlap", 7.5),
        _verdict("none", 8.5),
        _verdict("narrower_than", 8.5),
    ])
    a = attribute_mapping(mongo_db, "MPL-117", "MPL-038", adapter,
                          passes=3, apply=True)
    assert a.status == "unresolved"

    repo = MappingRepository(mongo_db)
    before = repo.get_pair("MPL-117", "MPL-038")
    assert before.decided_via is None

    m = resolve_mapping(
        mongo_db, "MPL-117", "MPL-038",
        verdict="none", rationale="lexical pull, not a real relation",
        decided_via="operator_delegate",
    )
    assert m.status == "rejected"
    assert m.relationship == "none"
    assert m.relationship_id is None
    assert m.decided_via == "operator_delegate"
    assert m.decision_rationale == "lexical pull, not a real relation"
    assert m.decided_at is not None
    # The model-consensus audit survives the operator verdict.
    assert m.per_pass == before.per_pass
    assert m.models_used == before.models_used

    # An accepting verdict must name a real taxonomy relation.
    m2 = resolve_mapping(
        mongo_db, "MPL-117", "MPL-038",
        verdict="partial_overlap", rationale="on reflection, overlaps",
    )
    assert m2.status == "accepted"
    assert m2.relationship == "partial_overlap"
    assert m2.relationship_id is not None

    # Garbage verdict and missing pair both fail closed.
    import pytest
    with pytest.raises(ResolutionError):
        resolve_mapping(mongo_db, "MPL-117", "MPL-038",
                        verdict="sorta_related", rationale="x")
    with pytest.raises(ResolutionError):
        resolve_mapping(mongo_db, "MPL-117", "MPL-999",
                        verdict="none", rationale="x")


def test_mappings_go_stale_when_endpoint_changes(mongo_db) -> None:
    from mahalath.staleness import mark_dependents_stale

    seed_mapping_relations(mongo_db)
    _seed_pair(mongo_db)
    adapter = _SeqAdapter(responses=[_verdict("partial_overlap", 9.0)] * 3)
    attribute_mapping(mongo_db, "MPL-117", "MPL-038", adapter,
                      passes=3, apply=True)
    assert MappingRepository(mongo_db).get_pair(
        "MPL-117", "MPL-038").is_stale is False

    mark_dependents_stale(mongo_db, "MPL-038",
                          change_type="definition_redefined", note="test")
    stored = MappingRepository(mongo_db).get_pair("MPL-117", "MPL-038")
    assert stored.is_stale is True
    assert stored.stale_reasons[-1]["upstream_label"] == "MPL-038"


def test_prompts_carry_task_markers(mongo_db) -> None:
    seed_mapping_relations(mongo_db)
    _seed_pair(mongo_db)
    from mahalath.db.repositories import DefinitionContextRepository
    from mahalath.mappings import (
        build_attribution_prompt,
        build_candidate_prompt,
    )
    repo = OntologyEntryRepository(mongo_db)
    src, tgt = repo.get("MPL-117"), repo.get("MPL-038")
    relations = DefinitionContextRepository(mongo_db).all(
        kind="mapping_relation")
    assert build_candidate_prompt(src, [tgt]).startswith(CANDIDATE_TAG)
    p = build_attribution_prompt(src, tgt, relations)
    assert p.startswith(ATTRIBUTION_TAG)
    assert "narrower_than" in p and "none" in p
