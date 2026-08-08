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
    retro_link_new_entry,
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


# --- Retro-link on insert ---------------------------------------------------


def test_retro_link_updates_older_entries(mongo_db) -> None:
    # MPL-001 was written when "vorton" didn't exist as an entry, so its
    # references are (correctly, at the time) empty.
    _seed(mongo_db, "MPL-001", "weave",
          definitions=["A weave is a braid of vorton threads."])
    update_references(mongo_db, "MPL-001")
    assert OntologyEntryRepository(mongo_db).get("MPL-001").references_labels == []

    # The vorton entry lands later; retro-link closes the reverse index.
    _seed(mongo_db, "MPL-002", "vorton")
    linked = retro_link_new_entry(mongo_db, "MPL-002")
    assert linked == ["MPL-001"]
    assert OntologyEntryRepository(mongo_db).get("MPL-001").references_labels == ["MPL-002"]
    assert [e.mpl_label for e in entries_referencing(mongo_db, "MPL-002")] == ["MPL-001"]


def test_retro_link_short_term_skipped(mongo_db) -> None:
    # < 4 chars would over-match common words; same threshold as
    # semantic matching.
    _seed(mongo_db, "MPL-001", "weave", definitions=["An ion moves."])
    _seed(mongo_db, "MPL-002", "ion")
    assert retro_link_new_entry(mongo_db, "MPL-002") == []


def test_retro_link_no_mentions_is_noop(mongo_db) -> None:
    _seed(mongo_db, "MPL-001", "weave", definitions=["Nothing relevant."])
    _seed(mongo_db, "MPL-002", "vorton")
    assert retro_link_new_entry(mongo_db, "MPL-002") == []
    assert OntologyEntryRepository(mongo_db).get("MPL-001").references_labels == []


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


def test_audit_clears_when_consistent_at_threshold(mongo_db, mongo_config) -> None:
    import json
    from mahalath.adapters import MockAdapter
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
    config = mongo_config
    result = audit_pending_stale(config, mongo_db, adapter, max_items=5)

    assert result.items_audited == 1
    assert result.items_cleared == 1
    stored = OntologyEntryRepository(mongo_db).get("MPL-002")
    assert stored.is_stale is False
    assert stored.stale_reasons == []


def test_audit_keeps_stale_when_inconsistent(mongo_db, mongo_config) -> None:
    import json
    from mahalath.adapters import MockAdapter
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
    config = mongo_config
    result = audit_pending_stale(config, mongo_db, adapter)

    assert result.items_still_stale == 1
    assert result.items_cleared == 0
    stored = OntologyEntryRepository(mongo_db).get("MPL-002")
    assert stored.is_stale is True
    # The audit verdict was appended as an extra stale_reason
    assert any(
        r.get("change_type") == "audit_inconsistent" for r in stored.stale_reasons
    )


def test_audit_keeps_stale_when_below_threshold(mongo_db, mongo_config) -> None:
    """Consistent verdict but low confidence → keep stale for safety."""
    import json
    from mahalath.adapters import MockAdapter
    from mahalath.staleness import audit_pending_stale, mark_dependents_stale, update_references

    _seed(mongo_db, "MPL-001", "alpha")
    _seed(mongo_db, "MPL-002", "beta", definitions=["mentions alpha"])
    update_references(mongo_db, "MPL-002")
    mark_dependents_stale(mongo_db, "MPL-001", change_type="x")

    verdict = json.dumps({"decision": "consistent", "confidence": 5.0, "reasoning": "shrug"})
    adapter = MockAdapter(default_response=verdict)
    config = mongo_config
    result = audit_pending_stale(config, mongo_db, adapter)
    assert result.items_cleared == 0
    assert result.items_still_stale == 1


def test_audit_unclear_routes_to_keep_stale(mongo_db, mongo_config) -> None:
    import json
    from mahalath.adapters import MockAdapter
    from mahalath.staleness import audit_pending_stale, mark_dependents_stale, update_references

    _seed(mongo_db, "MPL-001", "alpha")
    _seed(mongo_db, "MPL-002", "beta", definitions=["mentions alpha"])
    update_references(mongo_db, "MPL-002")
    mark_dependents_stale(mongo_db, "MPL-001", change_type="x")

    verdict = json.dumps({"decision": "unclear", "confidence": 9.0, "reasoning": "ambiguous"})
    adapter = MockAdapter(default_response=verdict)
    config = mongo_config
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


def test_redefine_cascades_dependents(mongo_db) -> None:
    """#21 / #2: a successful redefine marks downstream dependents stale."""
    import json

    from mahalath.adapters import MockAdapter
    from mahalath.db.repositories import OntologyEntryRepository
    from mahalath.staleness import redefine_stale_entry, update_references

    _seed(mongo_db, "MPL-001", "alpha", definitions=["alpha sense v1"])
    _seed(mongo_db, "MPL-002", "beta", definitions=["MPL-001 reference."])
    _seed(mongo_db, "MPL-003", "gamma", definitions=["MPL-002 reference."])
    update_references(mongo_db, "MPL-002")
    update_references(mongo_db, "MPL-003")

    # Mark alpha stale so redefine_stale_entry is a realistic call path.
    mongo_db.ontology_entries.update_one(
        {"_id": "MPL-001"},
        {"$set": {"is_stale": True}, "$push": {"stale_reasons": {"change_type": "x"}}},
    )
    entry = OntologyEntryRepository(mongo_db).get("MPL-001")
    adapter = MockAdapter(
        default_response=json.dumps(
            {
                "new_definition": "alpha sense v2 (redefined)",
                "confidence": 8.5,
                "rationale": "sharper boundary",
            }
        )
    )
    verdict = redefine_stale_entry(entry, mongo_db, adapter, min_confidence=6.0)
    assert verdict is not None
    assert verdict.noop is False
    assert verdict.decision_log_id is not None
    stored = OntologyEntryRepository(mongo_db).get("MPL-001")
    assert stored.is_stale is False
    # H3: appended definition links to the decision log.
    assert stored.definitions[-1].decision_log_id == verdict.decision_log_id
    assert mongo_db.decision_log.find_one(
        {"decision_log_id": verdict.decision_log_id}
    ) is not None
    assert mongo_db.agent_exchanges.find_one(
        {"decision_log_id": verdict.decision_log_id}
    ) is not None
    # Dependents that referenced the redefined label must now be stale.
    beta = OntologyEntryRepository(mongo_db).get("MPL-002")
    gamma = OntologyEntryRepository(mongo_db).get("MPL-003")
    assert beta.is_stale is True
    assert gamma.is_stale is True
    assert any(
        r.get("change_type") == "definition_redefined" for r in beta.stale_reasons
    )


def test_redefine_noop_same_frame_skips_append_and_cascade(mongo_db) -> None:
    """H1/H2/M2: identical same-frame text clears stale without write/cascade."""
    import json

    from mahalath.adapters import MockAdapter
    from mahalath.db.repositories import OntologyEntryRepository
    from mahalath.staleness import redefine_stale_entry, update_references

    same = "alpha is the foundational sense"
    _seed(mongo_db, "MPL-001", "alpha", definitions=[same])
    _seed(mongo_db, "MPL-002", "beta", definitions=["MPL-001 reference."])
    update_references(mongo_db, "MPL-002")

    mongo_db.ontology_entries.update_one(
        {"_id": "MPL-001"},
        {
            "$set": {"is_stale": True},
            "$push": {"stale_reasons": {"change_type": "audit_inconsistent"}},
        },
    )
    entry = OntologyEntryRepository(mongo_db).get("MPL-001")
    # Whitespace-variant of the existing definition → still a no-op.
    adapter = MockAdapter(
        default_response=json.dumps(
            {
                "new_definition": "  alpha is the   foundational sense  ",
                "confidence": 9.0,
                "rationale": "unchanged after re-derivation",
            }
        )
    )
    verdict = redefine_stale_entry(entry, mongo_db, adapter, min_confidence=6.0)
    assert verdict is not None
    assert verdict.noop is True
    assert verdict.decision_log_id is not None

    stored = OntologyEntryRepository(mongo_db).get("MPL-001")
    assert stored.is_stale is False
    assert stored.stale_reasons == []
    assert len(stored.definitions) == 1  # no duplicate append
    # Cascade must not fire.
    beta = OntologyEntryRepository(mongo_db).get("MPL-002")
    assert beta.is_stale is False
    # Audit row still written for the redefine attempt.
    assert mongo_db.decision_log.find_one(
        {"decision_log_id": verdict.decision_log_id}
    ) is not None


def test_dedupe_identical_frame_definitions(mongo_db) -> None:
    """F2: drop later exact-duplicate definitions within one frame."""
    from mahalath.staleness import dedupe_identical_frame_definitions

    _seed(
        mongo_db,
        "MPL-001",
        "alpha",
        definitions=[
            "same text in structural",
            "different text",
            "same text in structural",  # duplicate of first (both context_id None)
        ],
    )
    dry = dedupe_identical_frame_definitions(mongo_db, dry_run=True)
    assert dry.entries_with_duplicates == 1
    assert dry.definitions_removed == 1
    # Dry-run leaves data alone.
    assert len(OntologyEntryRepository(mongo_db).get("MPL-001").definitions) == 3

    applied = dedupe_identical_frame_definitions(mongo_db, dry_run=False)
    assert applied.definitions_removed == 1
    stored = OntologyEntryRepository(mongo_db).get("MPL-001")
    assert len(stored.definitions) == 2
    assert [d.text for d in stored.definitions] == [
        "same text in structural",
        "different text",
    ]


def test_redefine_appends_def_and_clears_stale(mongo_db, mongo_config) -> None:
    import json
    from mahalath.adapters import MockAdapter
    from mahalath.staleness import (
        redefine_pending_stale, mark_dependents_stale, update_references,
    )

    _seed(mongo_db, "MPL-001", "alpha")
    _seed(mongo_db, "MPL-002", "beta", definitions=["depends on alpha"])
    update_references(mongo_db, "MPL-002")
    mark_dependents_stale(mongo_db, "MPL-001", change_type="audit_inconsistent",
                          note="audit said inconsistent")
    # Manually inject audit_inconsistent reason
    mongo_db.ontology_entries.update_one(
        {"_id": "MPL-002"},
        {"$push": {"stale_reasons": {
            "upstream_label": None,
            "change_type": "audit_inconsistent",
            "changed_at": None,
            "note": "fake audit verdict",
        }}}
    )

    verdict = json.dumps({
        "new_definition": "Beta is the second concept that builds on alpha.",
        "confidence": 8.0,
        "rationale": "Updated to reflect alpha's current state.",
    })
    adapter = MockAdapter(default_response=verdict)
    config = mongo_config
    result = redefine_pending_stale(config, mongo_db, adapter, max_items=5)

    assert result.items_redefined == 1
    assert result.items_skipped == 0
    stored = OntologyEntryRepository(mongo_db).get("MPL-002")
    assert stored.is_stale is False
    assert stored.stale_reasons == []
    assert len(stored.definitions) == 2
    assert stored.definitions[-1].model_used == "rem_redefine (mock-model)"
    assert "second concept" in stored.definitions[-1].text


def test_redefine_skips_when_below_min_confidence(mongo_db, mongo_config) -> None:
    import json
    from mahalath.adapters import MockAdapter
    from mahalath.staleness import (
        redefine_pending_stale, mark_dependents_stale, update_references,
    )

    _seed(mongo_db, "MPL-001", "alpha")
    _seed(mongo_db, "MPL-002", "beta", definitions=["depends on alpha"])
    update_references(mongo_db, "MPL-002")
    mark_dependents_stale(mongo_db, "MPL-001", change_type="x")
    mongo_db.ontology_entries.update_one(
        {"_id": "MPL-002"},
        {"$push": {"stale_reasons": {
            "change_type": "audit_inconsistent", "note": "ok",
        }}}
    )

    verdict = json.dumps({
        "new_definition": "shaky text",
        "confidence": 4.0,
        "rationale": "not sure",
    })
    adapter = MockAdapter(default_response=verdict)
    config = mongo_config
    result = redefine_pending_stale(
        config, mongo_db, adapter, min_confidence=6.0,
    )
    assert result.items_redefined == 0
    assert result.items_skipped == 1
    # Stale flag remains; no new definition written
    stored = OntologyEntryRepository(mongo_db).get("MPL-002")
    assert stored.is_stale is True
    assert len(stored.definitions) == 1


def test_redefine_only_picks_audit_flagged_items(mongo_db, mongo_config) -> None:
    """Stale entries without an audit verdict are NOT redefined yet."""
    import json
    from mahalath.adapters import MockAdapter
    from mahalath.staleness import (
        redefine_pending_stale, mark_dependents_stale, update_references,
    )

    _seed(mongo_db, "MPL-001", "alpha")
    _seed(mongo_db, "MPL-002", "beta", definitions=["mentions alpha"])
    update_references(mongo_db, "MPL-002")
    # Mark stale via a structural change — no audit reason yet
    mark_dependents_stale(mongo_db, "MPL-001", change_type="reparented")

    adapter = MockAdapter(default_response=json.dumps({
        "new_definition": "x", "confidence": 9.0, "rationale": "x",
    }))
    config = mongo_config
    result = redefine_pending_stale(config, mongo_db, adapter)
    # No item picked up because no audit_inconsistent reason
    assert result.items_at_start == 0
    assert result.items_redefined == 0


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


# --- Definition-context backfill ------------------------------------------


def _seed_contexts(mongo_db, *specs) -> dict[str, str]:
    """Insert DefinitionContexts; return {name: context_id}."""
    from mahalath.db.models import DefinitionContext
    from mahalath.db.repositories import DefinitionContextRepository

    repo = DefinitionContextRepository(mongo_db)
    ids: dict[str, str] = {}
    for name, description in specs:
        ctx = DefinitionContext(name=name, description=description)
        repo.insert(ctx)
        ids[name] = ctx.context_id
    return ids


def test_backfill_contexts_dry_run_proposes_without_writing(mongo_config, mongo_db) -> None:
    import json
    from mahalath.adapters import MockAdapter
    from mahalath.staleness import backfill_definition_contexts

    _seed_contexts(
        mongo_db,
        ("theological", "Definitions in the corpus's biblical framing."),
        ("structural", "Definitions in the generic structural framing."),
    )
    _seed(mongo_db, "MPL-001", "alpha", definitions=["an untagged definition"])
    _seed(mongo_db, "MPL-002", "beta", definitions=["another untagged definition"])

    verdict = json.dumps({"context_name": "theological", "rationale": "fits"})
    adapter = MockAdapter(default_response=verdict)

    result = backfill_definition_contexts(
        mongo_config, mongo_db, adapter, max_items=50, apply=False,
    )

    assert result.untagged_at_start == 2
    assert result.proposals_generated == 2
    assert result.applied == 0
    assert all(p.source == "model" for p in result.proposals)
    assert all(p.proposed_context_name == "theological" for p in result.proposals)
    # Nothing written to disk in dry-run.
    for label in ("MPL-001", "MPL-002"):
        stored = OntologyEntryRepository(mongo_db).get(label)
        assert stored.definitions[0].context_id is None


def test_backfill_contexts_apply_writes_context_id(mongo_config, mongo_db) -> None:
    import json
    from mahalath.adapters import MockAdapter
    from mahalath.staleness import backfill_definition_contexts

    ids = _seed_contexts(
        mongo_db,
        ("theological", "Definitions in the corpus's biblical framing."),
    )
    _seed(mongo_db, "MPL-001", "alpha", definitions=["an untagged definition"])

    verdict = json.dumps({"context_name": "theological", "rationale": "fits"})
    adapter = MockAdapter(default_response=verdict)

    result = backfill_definition_contexts(
        mongo_config, mongo_db, adapter, max_items=50, apply=True,
    )

    assert result.applied == 1
    stored = OntologyEntryRepository(mongo_db).get("MPL-001")
    assert stored.definitions[0].context_id == ids["theological"]


def test_backfill_contexts_keyword_fallback_on_null_verdict(mongo_config, mongo_db) -> None:
    import json
    from mahalath.adapters import MockAdapter
    from mahalath.staleness import backfill_definition_contexts

    _seed_contexts(
        mongo_db,
        ("physical", "Vorton knot configuration within the substrate medium."),
        ("theological", "Biblical scriptural creaturely framing of meaning."),
    )
    _seed(mongo_db, "MPL-001", "vorton",
          definitions=["A vorton is a knot configuration within the substrate."])

    # Model declines to choose; fallback should match on token overlap.
    adapter = MockAdapter(default_response=json.dumps({"context_name": None}))

    result = backfill_definition_contexts(
        mongo_config, mongo_db, adapter, max_items=50, apply=True,
    )

    assert result.proposals_generated == 1
    assert result.proposals[0].source == "keyword_fallback"
    assert result.proposals[0].proposed_context_name == "physical"
    assert result.applied == 1


def test_backfill_contexts_respects_max_items(mongo_config, mongo_db) -> None:
    import json
    from mahalath.adapters import MockAdapter
    from mahalath.staleness import backfill_definition_contexts

    _seed_contexts(mongo_db, ("general", "Default frame."))
    _seed(mongo_db, "MPL-001", "alpha", definitions=["untagged one"])
    _seed(mongo_db, "MPL-002", "beta", definitions=["untagged two"])
    _seed(mongo_db, "MPL-003", "gamma", definitions=["untagged three"])

    adapter = MockAdapter(default_response=json.dumps({"context_name": "general"}))
    result = backfill_definition_contexts(
        mongo_config, mongo_db, adapter, max_items=2, apply=False,
    )

    assert result.untagged_at_start == 3
    assert result.proposals_generated == 2


def test_backfill_contexts_noop_when_no_contexts_defined(mongo_config, mongo_db) -> None:
    from mahalath.adapters import MockAdapter
    from mahalath.staleness import backfill_definition_contexts

    _seed(mongo_db, "MPL-001", "alpha", definitions=["untagged"])
    adapter = MockAdapter(default_response="ok")

    result = backfill_definition_contexts(
        mongo_config, mongo_db, adapter, max_items=50, apply=False,
    )

    assert result.untagged_at_start == 0
    assert result.proposals_generated == 0
    assert adapter.calls == []


def test_backfill_contexts_only_labels_scopes_the_scan(mongo_config, mongo_db) -> None:
    """only_labels restricts the walk to the named entries (pipeline scoping)."""
    import json
    from mahalath.adapters import MockAdapter
    from mahalath.staleness import backfill_definition_contexts

    _seed_contexts(mongo_db, ("general", "Default frame."))
    _seed(mongo_db, "MPL-001", "alpha", definitions=["untagged one"])
    _seed(mongo_db, "MPL-002", "beta", definitions=["untagged two"])

    adapter = MockAdapter(default_response=json.dumps({"context_name": "general"}))
    result = backfill_definition_contexts(
        mongo_config, mongo_db, adapter, apply=True, only_labels={"MPL-001"},
    )

    # Only MPL-001 was in scope, so only it counts as untagged + applied.
    assert result.untagged_at_start == 1
    assert result.applied == 1
    assert [p.mpl_label for p in result.proposals] == ["MPL-001"]
    # MPL-002 was never touched.
    assert OntologyEntryRepository(mongo_db).get("MPL-002").definitions[0].context_id is None


# --- Redefine-tail intent backfill ------------------------------------------


def _seed_stale_inconsistent(mongo_db, label="MPL-002", term="beta") -> None:
    _seed(mongo_db, label, term, definitions=["depends on alpha"])
    mongo_db.ontology_entries.update_one(
        {"_id": label},
        {"$set": {"is_stale": True},
         "$push": {"stale_reasons": {
             "upstream_label": None,
             "change_type": "audit_inconsistent",
             "changed_at": None,
             "note": "fake audit verdict",
         }}},
    )


def _redefine_adapter_with_intents():
    import json
    from mahalath.adapters import MockAdapter
    from mahalath.intents import INTENT_ATTRIBUTION_TAG

    return MockAdapter(
        default_response="{}",
        responses={
            "ontology definition editor": json.dumps({
                "new_definition": "Beta builds on alpha's current form.",
                "confidence": 8.0,
                "rationale": "tracks upstream",
            }),
            INTENT_ATTRIBUTION_TAG: json.dumps({
                "intent_tags": ["teach"],
                "intentionality": "high",
                "confidence": 9.0,
            }),
        },
    )


def test_redefine_runs_scoped_intent_backfill(mongo_db, mongo_config) -> None:
    from mahalath.db.models import DefinitionContext
    from mahalath.db.repositories import DefinitionContextRepository
    from mahalath.staleness import redefine_pending_stale

    ctx_repo = DefinitionContextRepository(mongo_db)
    teach = ctx_repo.insert(DefinitionContext(
        name="teach", description="instructs", kind="intent",
    ))
    _seed_stale_inconsistent(mongo_db)
    config = mongo_config

    result = redefine_pending_stale(
        config, mongo_db, _redefine_adapter_with_intents(), max_items=5,
    )

    assert result.items_redefined == 1
    assert result.intent_backfill is not None
    # Both the original (unattributed) and the appended definition are
    # in scope — backfill sweeps every unannotated def on the entry.
    assert result.intent_backfill["attempted"] == 2
    assert result.intent_backfill["stored"] == 2
    stored = OntologyEntryRepository(mongo_db).get("MPL-002")
    new_def = stored.definitions[-1]
    assert new_def.intent_tags == [teach.context_id]
    assert new_def.intentionality == "high"
    assert new_def.intent_confidence == 9.0


def test_redefine_intent_backfill_opt_out(mongo_db, mongo_config) -> None:
    from mahalath.intents import INTENT_ATTRIBUTION_TAG
    from mahalath.db.models import DefinitionContext
    from mahalath.db.repositories import DefinitionContextRepository
    from mahalath.staleness import redefine_pending_stale

    DefinitionContextRepository(mongo_db).insert(DefinitionContext(
        name="teach", description="instructs", kind="intent",
    ))
    _seed_stale_inconsistent(mongo_db)
    adapter = _redefine_adapter_with_intents()
    config = mongo_config

    result = redefine_pending_stale(
        config, mongo_db, adapter, max_items=5, intent_backfill=False,
    )

    assert result.items_redefined == 1
    assert result.intent_backfill is None
    assert not any(
        INTENT_ATTRIBUTION_TAG in c["prompt"] for c in adapter.calls
    )
    stored = OntologyEntryRepository(mongo_db).get("MPL-002")
    assert stored.definitions[-1].intent_tags == []


def test_redefine_intent_backfill_noop_without_taxonomy(mongo_db, mongo_config) -> None:
    from mahalath.intents import INTENT_ATTRIBUTION_TAG
    from mahalath.staleness import redefine_pending_stale

    _seed_stale_inconsistent(mongo_db)
    adapter = _redefine_adapter_with_intents()
    config = mongo_config

    result = redefine_pending_stale(config, mongo_db, adapter, max_items=5)

    assert result.items_redefined == 1
    # Backfill ran but found no intent taxonomy: zero attempts, no
    # adapter calls for attribution.
    assert result.intent_backfill is not None
    assert result.intent_backfill["attempted"] == 0
    assert not any(
        INTENT_ATTRIBUTION_TAG in c["prompt"] for c in adapter.calls
    )
