"""Reference tracking + staleness tests against a live MongoDB."""

from __future__ import annotations

import pytest

from mahalath.db.models import (
    DefinitionVersion,
    OntologyEntry,
)
from mahalath.db.repositories import OntologyEntryRepository
from mahalath.staleness import (
    append_operator_definition,
    backfill_references,
    clear_stale,
    compute_references_for_entry,
    entries_referencing,
    extract_references,
    list_stale,
    mark_dependents_stale,
    update_references,
)


def _seed(mongo_db, label, term, *, definitions=None, parent=None) -> OntologyEntry:
    defs = [DefinitionVersion(text=t) for t in (definitions or ["x"])]
    entry = OntologyEntry(
        mpl_label=label,
        canonical_term=term,
        confidence=8.0,
        parent_label=parent,
        definitions=defs,
    )
    OntologyEntryRepository(mongo_db).insert(entry)
    return entry


# --- Extraction -----------------------------------------------------------


def test_extract_references_finds_mpl_tokens() -> None:
    text = "This entry relates to MPL-001 and MPL-002.003 as defined above."
    assert extract_references(text) == ["MPL-001", "MPL-002.003"]


def test_extract_references_dedupes() -> None:
    text = "MPL-001 is referenced; MPL-001 again; finally MPL-002."
    assert extract_references(text) == ["MPL-001", "MPL-002"]


def test_extract_references_handles_variant_suffix() -> None:
    text = "See MPL-001.002.003a for the variant."
    assert extract_references(text) == ["MPL-001.002.003a"]


def test_extract_references_empty_input() -> None:
    assert extract_references("") == []
    assert extract_references("no labels here at all") == []


def test_extract_references_ignores_malformed() -> None:
    # Missing zero-padding shouldn't match (MPL-1 vs MPL-001).
    assert extract_references("MPL-1 is not a real label") == []


def test_semantic_extraction_matches_canonical_terms() -> None:
    from mahalath.staleness import extract_semantic_references
    index = {
        "relational substrate": "MPL-001",
        "substrate": "MPL-004",
        "ontology": "MPL-048",
    }
    text = "The Relational Substrate is the specific substrate that defines ontology."
    refs = extract_semantic_references(text, index, self_label="MPL-999")
    assert set(refs) == {"MPL-001", "MPL-004", "MPL-048"}


def test_semantic_extraction_excludes_self() -> None:
    from mahalath.staleness import extract_semantic_references
    index = {"substrate": "MPL-004", "relational substrate": "MPL-001"}
    text = "Substrate is the generic medium."
    refs = extract_semantic_references(text, index, self_label="MPL-004")
    assert refs == []


def test_semantic_extraction_respects_word_boundaries() -> None:
    from mahalath.staleness import extract_semantic_references
    index = {"substrate": "MPL-004"}
    # "substrateaceous" embeds the substring but \b boundary blocks it.
    text = "Substrateaceous things abound."
    refs = extract_semantic_references(text, index)
    assert refs == []


def test_semantic_extraction_excludes_short_terms() -> None:
    """Sub-threshold canonical terms are skipped to avoid over-matching common words."""
    from mahalath.staleness import extract_semantic_references
    index = {"to": "MPL-A", "if": "MPL-B"}  # both < 4 chars
    text = "to be or not to be, if it matters"
    refs = extract_semantic_references(text, index)
    assert refs == []


def test_compute_references_excludes_self() -> None:
    entry = OntologyEntry(
        mpl_label="MPL-001",
        canonical_term="x",
        confidence=8.0,
        definitions=[
            DefinitionVersion(text="MPL-001 is the entry being defined; references MPL-002."),
        ],
    )
    refs = compute_references_for_entry(entry)
    assert refs == ["MPL-002"]


# --- Reverse-index queries ------------------------------------------------


def test_entries_referencing(mongo_db) -> None:
    _seed(mongo_db, "MPL-001", "alpha", definitions=["mentions MPL-002."])
    _seed(mongo_db, "MPL-002", "beta")
    update_references(mongo_db, "MPL-001")
    found = entries_referencing(mongo_db, "MPL-002")
    assert [e.mpl_label for e in found] == ["MPL-001"]


def test_entries_referencing_returns_empty(mongo_db) -> None:
    _seed(mongo_db, "MPL-001", "alpha")
    assert entries_referencing(mongo_db, "MPL-999") == []


# --- Update + backfill ----------------------------------------------------


def test_update_references_writes_back(mongo_db) -> None:
    _seed(mongo_db, "MPL-001", "alpha", definitions=["mentions MPL-002 and MPL-003."])
    refs = update_references(mongo_db, "MPL-001")
    assert refs == ["MPL-002", "MPL-003"]
    stored = OntologyEntryRepository(mongo_db).get("MPL-001")
    assert stored.references_labels == ["MPL-002", "MPL-003"]


def test_backfill_references_populates_all(mongo_db) -> None:
    _seed(mongo_db, "MPL-001", "alpha", definitions=["see MPL-002."])
    _seed(mongo_db, "MPL-002", "beta", definitions=["see MPL-003."])
    _seed(mongo_db, "MPL-003", "gamma")
    result = backfill_references(mongo_db)
    assert result["scanned"] == 3
    assert result["updated"] == 2
    assert result["total_refs"] == 2
    assert OntologyEntryRepository(mongo_db).get("MPL-001").references_labels == ["MPL-002"]


def test_backfill_uses_semantic_matching(mongo_db) -> None:
    """Entries that reference by canonical term (no MPL label string) still get picked up."""
    _seed(mongo_db, "MPL-001", "Relational Substrate")
    _seed(mongo_db, "MPL-002", "vortons",
          definitions=["A vorton is a knot configuration within the Relational Substrate."])
    result = backfill_references(mongo_db)
    assert result["updated"] >= 1
    # MPL-002's definition mentions "Relational Substrate" (MPL-001).
    refs = OntologyEntryRepository(mongo_db).get("MPL-002").references_labels
    assert "MPL-001" in refs


# --- Mark dependents stale ------------------------------------------------


def test_mark_dependents_stale_flags_direct_dependents(mongo_db) -> None:
    _seed(mongo_db, "MPL-001", "alpha")
    _seed(mongo_db, "MPL-002", "beta", definitions=["mentions MPL-001."])
    update_references(mongo_db, "MPL-002")

    affected = mark_dependents_stale(
        mongo_db, "MPL-001",
        change_type="definition_updated",
        note="alpha got a new definition",
    )
    assert affected == ["MPL-002"]
    stored = OntologyEntryRepository(mongo_db).get("MPL-002")
    assert stored.is_stale is True
    assert len(stored.stale_reasons) == 1
    assert stored.stale_reasons[0]["upstream_label"] == "MPL-001"


def test_mark_dependents_stale_cascades(mongo_db) -> None:
    _seed(mongo_db, "MPL-001", "alpha")
    _seed(mongo_db, "MPL-002", "beta", definitions=["MPL-001 reference."])
    _seed(mongo_db, "MPL-003", "gamma", definitions=["MPL-002 reference."])
    update_references(mongo_db, "MPL-002")
    update_references(mongo_db, "MPL-003")

    affected = mark_dependents_stale(
        mongo_db, "MPL-001", change_type="definition_updated"
    )
    assert set(affected) == {"MPL-002", "MPL-003"}
    assert OntologyEntryRepository(mongo_db).get("MPL-003").is_stale is True


def test_mark_dependents_stale_is_cycle_safe(mongo_db) -> None:
    _seed(mongo_db, "MPL-001", "alpha", definitions=["mentions MPL-002."])
    _seed(mongo_db, "MPL-002", "beta", definitions=["mentions MPL-001."])
    update_references(mongo_db, "MPL-001")
    update_references(mongo_db, "MPL-002")

    # The walk should terminate even though they reference each other.
    affected = mark_dependents_stale(
        mongo_db, "MPL-001", change_type="x", cascade=True
    )
    assert "MPL-002" in affected


def test_mark_dependents_stale_appends_reason_on_already_stale(mongo_db) -> None:
    _seed(mongo_db, "MPL-001", "alpha")
    _seed(mongo_db, "MPL-002", "beta", definitions=["mentions MPL-001."])
    update_references(mongo_db, "MPL-002")

    mark_dependents_stale(mongo_db, "MPL-001", change_type="first")
    mark_dependents_stale(mongo_db, "MPL-001", change_type="second")
    stored = OntologyEntryRepository(mongo_db).get("MPL-002")
    assert len(stored.stale_reasons) == 2
    assert stored.stale_reasons[0]["change_type"] == "first"
    assert stored.stale_reasons[1]["change_type"] == "second"


def test_mark_dependents_stale_no_cascade_when_disabled(mongo_db) -> None:
    _seed(mongo_db, "MPL-001", "alpha")
    _seed(mongo_db, "MPL-002", "beta", definitions=["mentions MPL-001."])
    _seed(mongo_db, "MPL-003", "gamma", definitions=["mentions MPL-002."])
    update_references(mongo_db, "MPL-002")
    update_references(mongo_db, "MPL-003")

    affected = mark_dependents_stale(
        mongo_db, "MPL-001", change_type="x", cascade=False
    )
    assert affected == ["MPL-002"]
    assert OntologyEntryRepository(mongo_db).get("MPL-003").is_stale is False


# --- Clear + list ---------------------------------------------------------


def test_clear_stale_unflags_and_drops_reasons(mongo_db) -> None:
    _seed(mongo_db, "MPL-001", "alpha")
    _seed(mongo_db, "MPL-002", "beta", definitions=["mentions MPL-001."])
    update_references(mongo_db, "MPL-002")
    mark_dependents_stale(mongo_db, "MPL-001", change_type="x")

    clear_stale(mongo_db, "MPL-002")
    stored = OntologyEntryRepository(mongo_db).get("MPL-002")
    assert stored.is_stale is False
    assert stored.stale_reasons == []


def test_list_stale_returns_only_flagged(mongo_db) -> None:
    _seed(mongo_db, "MPL-001", "alpha")
    _seed(mongo_db, "MPL-002", "beta", definitions=["mentions MPL-001."])
    _seed(mongo_db, "MPL-003", "gamma")
    update_references(mongo_db, "MPL-002")

    mark_dependents_stale(mongo_db, "MPL-001", change_type="x")
    stale = list_stale(mongo_db)
    assert {e.mpl_label for e in stale} == {"MPL-002"}


# --- Operator definition helper -------------------------------------------


def test_audit_clears_when_consistent_at_threshold(mongo_db) -> None:
    import json
    from mahalath.adapters import MockAdapter
    from mahalath.config import AppConfig, MongoConfig
    from mahalath.staleness import audit_pending_stale, mark_dependents_stale, update_references

    _seed(mongo_db, "MPL-001", "alpha", definitions=["foundational concept"])
    _seed(mongo_db, "MPL-002", "beta",
          definitions=["beta builds on alpha — see also."])
    update_references(mongo_db, "MPL-002")
    mark_dependents_stale(mongo_db, "MPL-001", change_type="definition_updated")
    # MPL-002 is now stale.

    verdict = json.dumps({
        "decision": "consistent",
        "confidence": 8.5,
        "reasoning": "beta's definition still holds under current alpha",
    })
    adapter = MockAdapter(default_response=verdict)
    config = AppConfig(mongo=MongoConfig(database="mahalath_pytest"))
    result = audit_pending_stale(config, mongo_db, adapter, max_items=5)

    assert result.items_audited == 1
    assert result.items_cleared == 1
    stored = OntologyEntryRepository(mongo_db).get("MPL-002")
    assert stored.is_stale is False
    assert stored.stale_reasons == []


def test_audit_keeps_stale_when_inconsistent(mongo_db) -> None:
    import json
    from mahalath.adapters import MockAdapter
    from mahalath.config import AppConfig, MongoConfig
    from mahalath.staleness import audit_pending_stale, mark_dependents_stale, update_references

    _seed(mongo_db, "MPL-001", "alpha")
    _seed(mongo_db, "MPL-002", "beta", definitions=["depends on alpha"])
    update_references(mongo_db, "MPL-002")
    mark_dependents_stale(mongo_db, "MPL-001", change_type="definition_updated")

    verdict = json.dumps({
        "decision": "inconsistent",
        "confidence": 9.0,
        "reasoning": "alpha was redefined to mean something else",
    })
    adapter = MockAdapter(default_response=verdict)
    config = AppConfig(mongo=MongoConfig(database="mahalath_pytest"))
    result = audit_pending_stale(config, mongo_db, adapter)

    assert result.items_still_stale == 1
    assert result.items_cleared == 0
    stored = OntologyEntryRepository(mongo_db).get("MPL-002")
    assert stored.is_stale is True
    # The audit verdict was appended as an extra stale_reason
    assert any(
        r.get("change_type") == "audit_inconsistent" for r in stored.stale_reasons
    )


def test_audit_keeps_stale_when_below_threshold(mongo_db) -> None:
    """Consistent verdict but low confidence → keep stale for safety."""
    import json
    from mahalath.adapters import MockAdapter
    from mahalath.config import AppConfig, MongoConfig
    from mahalath.staleness import audit_pending_stale, mark_dependents_stale, update_references

    _seed(mongo_db, "MPL-001", "alpha")
    _seed(mongo_db, "MPL-002", "beta", definitions=["mentions alpha"])
    update_references(mongo_db, "MPL-002")
    mark_dependents_stale(mongo_db, "MPL-001", change_type="x")

    verdict = json.dumps({"decision": "consistent", "confidence": 5.0, "reasoning": "shrug"})
    adapter = MockAdapter(default_response=verdict)
    config = AppConfig(mongo=MongoConfig(database="mahalath_pytest"))
    result = audit_pending_stale(config, mongo_db, adapter)
    assert result.items_cleared == 0
    assert result.items_still_stale == 1


def test_audit_unclear_routes_to_keep_stale(mongo_db) -> None:
    import json
    from mahalath.adapters import MockAdapter
    from mahalath.config import AppConfig, MongoConfig
    from mahalath.staleness import audit_pending_stale, mark_dependents_stale, update_references

    _seed(mongo_db, "MPL-001", "alpha")
    _seed(mongo_db, "MPL-002", "beta", definitions=["mentions alpha"])
    update_references(mongo_db, "MPL-002")
    mark_dependents_stale(mongo_db, "MPL-001", change_type="x")

    verdict = json.dumps({"decision": "unclear", "confidence": 9.0, "reasoning": "ambiguous"})
    adapter = MockAdapter(default_response=verdict)
    config = AppConfig(mongo=MongoConfig(database="mahalath_pytest"))
    result = audit_pending_stale(config, mongo_db, adapter)
    assert result.items_cleared == 0
    assert result.items_still_stale == 1


def test_parse_audit_verdict() -> None:
    import json
    from mahalath.staleness import parse_audit_verdict, AuditError
    v = parse_audit_verdict(json.dumps({
        "decision": "Consistent", "confidence": 8.0, "reasoning": "ok",
    }))
    assert v.decision == "consistent"  # case-folded
    assert v.confidence == 8.0

    with pytest.raises(AuditError):
        parse_audit_verdict(json.dumps({"decision": "yes", "confidence": 9}))


def test_append_operator_definition_updates_refs_and_cascades(mongo_db) -> None:
    _seed(mongo_db, "MPL-001", "alpha")
    _seed(mongo_db, "MPL-002", "beta", definitions=["mentions MPL-001."])
    update_references(mongo_db, "MPL-002")

    # Add operator definition to MPL-001; MPL-002 referenced it, so should go stale.
    append_operator_definition(
        mongo_db, "MPL-001",
        "Refined definition that mentions MPL-003.",
        note="manual correction",
    )
    stored_001 = OntologyEntryRepository(mongo_db).get("MPL-001")
    assert len(stored_001.definitions) == 2
    assert stored_001.definitions[-1].model_used == "operator"
    assert "MPL-003" in stored_001.references_labels

    stored_002 = OntologyEntryRepository(mongo_db).get("MPL-002")
    assert stored_002.is_stale is True
    assert stored_002.stale_reasons[0]["upstream_label"] == "MPL-001"
    assert stored_002.stale_reasons[0]["change_type"] == "definition_updated"
