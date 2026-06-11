"""Bundle layer (S-C): build_bundle, reference closure, budget, rendering.

Runs against a live MongoDB test database; the `mongo_db` fixture calls
ensure_indexes, so the `$text` index used by fuzzy search is present.
"""

from __future__ import annotations

import json

from mahalath.db.models import (
    DefinitionContext,
    DefinitionVersion,
    OntologyEntry,
)
from mahalath.db.repositories import (
    DefinitionContextRepository,
    OntologyEntryRepository,
)
from mahalath.retrieval import (
    build_bundle,
    estimate_tokens,
    render_entry_lines,
    Meaning,
)


def _entry(repo, label, term, *, defs=None, refs=None, ctx=None,
           aliases=None, parent=None):
    definitions = [
        DefinitionVersion(text=t, model_used="m", context_id=ctx)
        for t in (defs or [f"{term} definition."])
    ]
    repo.insert(OntologyEntry(
        mpl_label=label, canonical_term=term, confidence=8.0,
        aliases=aliases or [], parent_label=parent,
        references_labels=refs or [], definitions=definitions,
    ))


def _seed_chain(mongo_db):
    """alpha -> refs beta -> refs gamma (transitive closure fodder)."""
    repo = OntologyEntryRepository(mongo_db)
    _entry(repo, "MPL-001", "alpha", refs=["MPL-002"])
    _entry(repo, "MPL-002", "beta", refs=["MPL-003"])
    _entry(repo, "MPL-003", "gamma")
    return repo


# --- estimate_tokens --------------------------------------------------------


def test_estimate_tokens_chars_over_four() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


# --- closure (ADR-023) ------------------------------------------------------


def test_bundle_closure_is_transitive(mongo_db) -> None:
    _seed_chain(mongo_db)
    bundle = build_bundle(mongo_db, ["alpha"])
    assert [e.mpl_label for e in bundle.entries] == ["MPL-001"]
    closure_labels = {c.mpl_label for c in bundle.closure}
    assert closure_labels == {"MPL-002", "MPL-003"}  # transitive


def test_bundle_closure_cycle_safe(mongo_db) -> None:
    repo = OntologyEntryRepository(mongo_db)
    _entry(repo, "MPL-001", "alpha", refs=["MPL-002"])
    _entry(repo, "MPL-002", "beta", refs=["MPL-001"])  # cycle
    bundle = build_bundle(mongo_db, ["alpha"])
    assert {c.mpl_label for c in bundle.closure} == {"MPL-002"}


def test_bundle_closure_skips_dangling_refs(mongo_db) -> None:
    repo = OntologyEntryRepository(mongo_db)
    _entry(repo, "MPL-001", "alpha", refs=["MPL-099"])  # MPL-099 absent
    bundle = build_bundle(mongo_db, ["alpha"])
    assert bundle.closure == []


# --- term + ref resolution --------------------------------------------------


def test_bundle_explicit_ref_and_term_mix(mongo_db) -> None:
    _seed_chain(mongo_db)
    bundle = build_bundle(mongo_db, ["MPL-003", "alpha"])
    labels = [e.mpl_label for e in bundle.entries]
    assert "MPL-003" in labels  # explicit handle
    assert "MPL-001" in labels  # term-resolved
    assert bundle.unresolved == []


def test_bundle_frame_scoped_ref_keeps_one_frame(mongo_db) -> None:
    ctx_repo = DefinitionContextRepository(mongo_db)
    ctx_st = DefinitionContext(name="structural", description="S.")
    ctx_th = DefinitionContext(name="theological", description="T.")
    ctx_repo.insert(ctx_st)
    ctx_repo.insert(ctx_th)
    repo = OntologyEntryRepository(mongo_db)
    repo.insert(OntologyEntry(
        mpl_label="MPL-001", canonical_term="substrate", confidence=8.0,
        definitions=[
            DefinitionVersion(text="Structural meaning.",
                              context_id=ctx_st.context_id),
            DefinitionVersion(text="Theological meaning.",
                              context_id=ctx_th.context_id),
        ],
    ))
    bundle = build_bundle(mongo_db, ["MPL-001#structural"])
    assert len(bundle.entries) == 1
    assert [m.context_name for m in bundle.entries[0].meanings] == ["structural"]


def test_bundle_term_match_carries_all_frames(mongo_db) -> None:
    # ADR-022: a term-resolved entry returns every frame.
    ctx_repo = DefinitionContextRepository(mongo_db)
    ctx_st = DefinitionContext(name="structural", description="S.")
    ctx_th = DefinitionContext(name="theological", description="T.")
    ctx_repo.insert(ctx_st)
    ctx_repo.insert(ctx_th)
    repo = OntologyEntryRepository(mongo_db)
    repo.insert(OntologyEntry(
        mpl_label="MPL-001", canonical_term="substrate", confidence=8.0,
        definitions=[
            DefinitionVersion(text="Structural meaning.",
                              context_id=ctx_st.context_id),
            DefinitionVersion(text="Theological meaning.",
                              context_id=ctx_th.context_id),
        ],
    ))
    bundle = build_bundle(mongo_db, ["substrate"])
    frames = {m.context_name for m in bundle.entries[0].meanings}
    assert frames == {"structural", "theological"}
    assert "structural" in bundle.as_text
    assert "theological" in bundle.as_text


def test_bundle_unresolved_inputs_recorded(mongo_db) -> None:
    _seed_chain(mongo_db)
    bundle = build_bundle(mongo_db, ["MPL-099", "nonexistentterm"])
    assert set(bundle.unresolved) == {"MPL-099", "nonexistentterm"}
    assert bundle.entries == []


def test_bundle_runner_up_matches_become_alternatives(mongo_db) -> None:
    repo = OntologyEntryRepository(mongo_db)
    _entry(repo, "MPL-001", "substrate")
    _entry(repo, "MPL-002", "relational substrate")
    bundle = build_bundle(mongo_db, ["substrate"])
    primary = [e.mpl_label for e in bundle.entries]
    alt = [a.mpl_label for a in bundle.alternatives]
    assert len(primary) == 1
    # The runner-up surfaces as an alternative, not silently dropped.
    assert set(primary + alt) >= {"MPL-001", "MPL-002"}


# --- budget -----------------------------------------------------------------


def test_bundle_budget_never_drops_closure(mongo_db) -> None:
    _seed_chain(mongo_db)
    bundle = build_bundle(mongo_db, ["alpha"], token_budget=10)  # absurdly small
    # Closure is mandatory (ADR-023): still present, only degraded.
    assert {c.mpl_label for c in bundle.closure} == {"MPL-002", "MPL-003"}
    assert bundle.degradations  # the ladder was exercised
    assert "MPL-002" in bundle.as_text
    assert "MPL-003" in bundle.as_text


def test_bundle_budget_trims_breadth_keeps_one_primary(mongo_db) -> None:
    repo = OntologyEntryRepository(mongo_db)
    long_def = "A deliberately verbose definition. " * 30
    _entry(repo, "MPL-001", "alpha", defs=[long_def])
    _entry(repo, "MPL-002", "beta", defs=[long_def])
    _entry(repo, "MPL-003", "gamma", defs=[long_def])
    bundle = build_bundle(
        mongo_db, ["alpha", "beta", "gamma"], token_budget=120,
    )
    assert len(bundle.entries) == 1  # trimmed to the floor, never zero
    assert "breadth_trimmed" in bundle.degradations
    # Trimmed primaries are surfaced as alternatives, not lost.
    assert len(bundle.alternatives) >= 2


def test_bundle_budget_never_drops_explicit_refs(mongo_db) -> None:
    repo = OntologyEntryRepository(mongo_db)
    long_def = "A deliberately verbose definition. " * 30
    _entry(repo, "MPL-001", "alpha", defs=[long_def])
    _entry(repo, "MPL-002", "beta", defs=[long_def])
    bundle = build_bundle(
        mongo_db, ["MPL-001", "MPL-002"], token_budget=50,
    )
    # Caller-chosen handles survive any budget.
    assert [e.mpl_label for e in bundle.entries] == ["MPL-001", "MPL-002"]


def test_bundle_within_budget_no_degradations(mongo_db) -> None:
    _seed_chain(mongo_db)
    bundle = build_bundle(mongo_db, ["alpha"], token_budget=100_000)
    assert bundle.degradations == []
    assert bundle.token_estimate <= 100_000


# --- rendering --------------------------------------------------------------


def test_bundle_as_text_idiom(mongo_db) -> None:
    _seed_chain(mongo_db)
    bundle = build_bundle(mongo_db, ["alpha"])
    text = bundle.as_text
    assert "CODIFIED MEANINGS" in text
    assert "--- MPL-001: alpha ---" in text
    assert "REFERENCED TERMS" in text
    assert text.index("MPL-001") < text.index("REFERENCED TERMS")
    assert bundle.token_estimate == estimate_tokens(text)


def test_bundle_to_dict_and_json_round_trip(mongo_db) -> None:
    _seed_chain(mongo_db)
    bundle = build_bundle(mongo_db, ["alpha"])
    payload = json.loads(bundle.as_json())
    assert payload["entries"][0]["mpl_label"] == "MPL-001"
    assert {c["mpl_label"] for c in payload["closure"]} == {"MPL-002", "MPL-003"}
    assert payload["token_budget"] == 1500
    assert payload["as_text"] == bundle.as_text


def test_render_entry_lines_compact_truncates() -> None:
    meanings = [Meaning(
        context_id="c1", context_name="structural",
        description="x" * 500, model_used="m",
        consensus_score=None, created_at=None,
    )]
    lines = render_entry_lines(
        mpl_label="MPL-001", canonical_term="alpha",
        meanings=meanings, compact=True, closure_text_cap=100,
    )
    assert lines[0] == "--- MPL-001: alpha ---"
    assert len(lines[1]) <= 100 + len("  [structural] ") + 1
    assert lines[1].endswith("…")


# --- propose_term (S-D) -----------------------------------------------------


from mahalath.retrieval import propose_term  # noqa: E402


def test_propose_term_existing_match_no_enqueue(mongo_db) -> None:
    repo = OntologyEntryRepository(mongo_db)
    _entry(repo, "MPL-001", "substrate")
    template = propose_term(mongo_db, "substrate")
    assert template.status == "existing"
    assert template.enqueued is False
    assert template.matches[0].mpl_label == "MPL-001"
    assert mongo_db.undecided_queue.count_documents({}) == 0


def test_propose_term_enqueues_with_resolving_decision_log(mongo_db) -> None:
    template = propose_term(
        mongo_db, "morphogenesis",
        context="Morphogenesis is the emergence of stable form.",
    )
    assert template.status == "enqueued"
    assert template.enqueued is True

    row = mongo_db.undecided_queue.find_one({"term": "morphogenesis"})
    assert row is not None
    assert row["reason"] == "proposed_term"
    assert row["context"] == "Morphogenesis is the emergence of stable form."
    # The queue row's decision_log_id resolves, like every other row.
    log = mongo_db.decision_log.find_one(
        {"decision_log_id": template.decision_log_id}
    )
    assert log is not None
    assert log["outcome"] == "undecided"


def test_propose_term_near_hint_folded_into_context(mongo_db) -> None:
    repo = OntologyEntryRepository(mongo_db)
    _entry(repo, "MPL-001", "substrate")
    template = propose_term(
        mongo_db, "morphogenesis",
        context="Emergence of stable form.",
        near="MPL-001",
    )
    row = mongo_db.undecided_queue.find_one({"term": "morphogenesis"})
    assert 'related to "substrate"' in row["context"]
    assert template.near == "MPL-001"


def test_propose_term_already_queued_no_duplicate(mongo_db) -> None:
    first = propose_term(mongo_db, "morphogenesis", context="ctx")
    second = propose_term(mongo_db, "Morphogenesis")  # case-insensitive
    assert second.status == "already_queued"
    assert second.decision_log_id == first.decision_log_id
    assert mongo_db.undecided_queue.count_documents({}) == 1


def test_propose_term_dry_run_writes_nothing(mongo_db) -> None:
    template = propose_term(mongo_db, "morphogenesis", enqueue=False)
    assert template.status == "template_only"
    assert mongo_db.undecided_queue.count_documents({}) == 0
    assert mongo_db.decision_log.count_documents({}) == 0


def test_propose_term_context_less_warns_in_note(mongo_db) -> None:
    template = propose_term(mongo_db, "morphogenesis")
    assert template.status == "enqueued"
    assert "WITHOUT context" in template.note


def test_propose_term_empty_raises(mongo_db) -> None:
    import pytest
    with pytest.raises(ValueError):
        propose_term(mongo_db, "   ")


def test_proposed_term_flows_through_rem_review(mongo_config, mongo_db) -> None:
    """The S-D integration claim: a proposed term with context is picked
    up by the EXISTING REM re-review and debated into the ontology."""
    import json as _json
    from mahalath.adapters import MockAdapter
    from mahalath.debate import (
        SPEAKER_TAG_PRECISION_CRITIC,
        SPEAKER_TAG_SYNTHESIS_EXPLORER,
    )
    from mahalath.rem import rem_review

    propose_term(
        mongo_db, "morphogenesis",
        context="Morphogenesis is the emergence of stable form.",
    )

    adapter = MockAdapter(responses={
        SPEAKER_TAG_PRECISION_CRITIC: _json.dumps({
            "definition": "The emergence of stable form.",
            "critique": "none", "confidence": 9.0,
        }),
        SPEAKER_TAG_SYNTHESIS_EXPLORER: _json.dumps({
            "definition": "The emergence of stable form within the weave.",
            "rationale": "agreed", "confidence": 9.0,
        }),
    })
    result = rem_review(mongo_config, mongo_db, adapter, max_items=5)
    assert result.items_accepted == 1
    assert mongo_db.undecided_queue.count_documents({}) == 0
    entry = OntologyEntryRepository(mongo_db).get(result.accepted_labels[0])
    assert entry.canonical_term == "morphogenesis"
    # And the definition carries the per-definition consensus score (S-D).
    assert entry.definitions[0].consensus_score == 9.0


# --- consensus_score capture (S-D) ------------------------------------------


def test_persist_writes_consensus_score(mongo_db) -> None:
    from mahalath.config import RuntimeConfig
    from mahalath.debate import DebateResult
    from mahalath.ontology import persist_debate_result

    result = DebateResult(
        decision_log_id="dl-cs", term="weave", source_document_id="doc-1",
        outcome="accepted", final_definition="A braid of threads.",
        final_confidence=8.5, iterations_used=2,
    )
    out = persist_debate_result(result, mongo_db, RuntimeConfig())
    entry = OntologyEntryRepository(mongo_db).get(out.mpl_label)
    assert entry.definitions[0].consensus_score == 8.5
    # Entry-level confidence matches at creation but is a separate field.
    assert entry.confidence == 8.5


def test_operator_definition_has_no_consensus_score(mongo_db) -> None:
    from mahalath.staleness import append_operator_definition

    repo = OntologyEntryRepository(mongo_db)
    _entry(repo, "MPL-001", "weave")
    append_operator_definition(mongo_db, "MPL-001", "Operator's wording.")
    entry = repo.get("MPL-001")
    assert entry.definitions[-1].model_used == "operator"
    assert entry.definitions[-1].consensus_score is None
