"""Detailed exposition of accepted definitions."""

from __future__ import annotations

import pytest

from mahalath.adapters import MockAdapter
from mahalath.config import AppConfig, RuntimeConfig
from mahalath.db.models import DefinitionVersion, OntologyEntry
from mahalath.db.repositories import OntologyEntryRepository
from mahalath.detailed import (
    DetailedError,
    backfill_detailed_definitions,
    enrich_definition_with_detail,
    generate_detailed_text,
    parse_detailed_response,
)
from mahalath.ontology import persist_debate_result
from mahalath.debate import DebateResult


_LONG = (
    "The Relational Substrate is the specific substrate whose constitution is "
    "fundamentally determined by relation. In this corpus it is not a passive "
    "backdrop: it is the medium in which admissible forms, closure rules, and "
    "resonance constraints are seated. Treat near-synonyms carefully — generic "
    "\"substrate\" is broader and less committed."
)


def test_parse_detailed_response_strips_fences_and_labels() -> None:
    raw = "```\nDetailed description: " + _LONG + "\n```"
    out = parse_detailed_response(raw)
    assert "Relational Substrate" in out
    assert "```" not in out


def test_parse_detailed_rejects_empty_and_short() -> None:
    with pytest.raises(DetailedError):
        parse_detailed_response("")
    with pytest.raises(DetailedError):
        parse_detailed_response("too short")


def test_generate_detailed_text_via_mock() -> None:
    adapter = MockAdapter(default_response=_LONG)
    text = generate_detailed_text(
        term="Relational Substrate",
        short_definition="The substrate whose constitution is determined by relation.",
        adapter=adapter,
    )
    assert "Relational Substrate" in text
    assert len(text) >= 80


def test_enrich_and_backfill(mongo_db) -> None:
    OntologyEntryRepository(mongo_db).insert(
        OntologyEntry(
            mpl_label="MPL-900",
            canonical_term="test term",
            confidence=8.0,
            definitions=[
                DefinitionVersion(
                    text="A short precise sense used for identity.",
                )
            ],
        )
    )
    adapter = MockAdapter(default_response=_LONG)
    written = enrich_definition_with_detail(
        mongo_db, "MPL-900", adapter=adapter, definition_index=0,
    )
    assert written is not None
    stored = OntologyEntryRepository(mongo_db).get("MPL-900")
    assert stored is not None
    assert stored.definitions[0].detailed_text
    assert "medium" in stored.definitions[0].detailed_text.lower() or len(
        stored.definitions[0].detailed_text
    ) >= 80

    # Second pass without overwrite is a no-op skip.
    again = enrich_definition_with_detail(
        mongo_db, "MPL-900", adapter=adapter, definition_index=0,
    )
    assert again == stored.definitions[0].detailed_text


def test_backfill_dry_run_and_apply(mongo_db) -> None:
    OntologyEntryRepository(mongo_db).insert(
        OntologyEntry(
            mpl_label="MPL-901",
            canonical_term="another",
            confidence=8.0,
            definitions=[DefinitionVersion(text="Short sense for another term here.")],
        )
    )
    config = AppConfig(runtime=RuntimeConfig(generate_detailed_definitions=True))
    adapter = MockAdapter(default_response=_LONG)
    dry = backfill_detailed_definitions(
        config, mongo_db, adapter, max_items=10, apply=False,
    )
    assert dry.definitions_missing >= 1
    assert dry.written == 0
    assert OntologyEntryRepository(mongo_db).get("MPL-901").definitions[0].detailed_text is None

    applied = backfill_detailed_definitions(
        config, mongo_db, adapter, max_items=10, apply=True,
    )
    assert applied.written >= 1
    assert OntologyEntryRepository(mongo_db).get("MPL-901").definitions[0].detailed_text


def test_persist_debate_attaches_detailed(mongo_db) -> None:
    adapter = MockAdapter(default_response=_LONG)
    result = DebateResult(
        decision_log_id="dl-detailed-1",
        term="sample concept",
        source_document_id="doc-1",
        outcome="accepted",
        final_definition="A short accepted sense of the sample concept.",
        final_confidence=8.5,
        iterations_used=1,
        context="Source prose that motivates the sample concept in the corpus.",
    )
    runtime = RuntimeConfig(generate_detailed_definitions=True)
    persisted = persist_debate_result(
        result, mongo_db, runtime, adapter=adapter,
    )
    assert persisted.outcome == "accepted"
    assert persisted.ontology_entry is not None
    detail = persisted.ontology_entry.definitions[0].detailed_text
    assert detail is not None
    assert len(detail) >= 80


def test_definition_version_accepts_detailed_text() -> None:
    d = DefinitionVersion(text="short", detailed_text="longer exposition " * 5)
    assert d.detailed_text is not None
    d2 = DefinitionVersion(text="short only")
    assert d2.detailed_text is None


# --- ADR-034: every exposition carries a readable conversation record ------


def _entry(label: str = "MPL-950", term: str = "relational substrate"):
    return OntologyEntry(
        mpl_label=label,
        canonical_term=term,
        confidence=8.5,
        definitions=[DefinitionVersion(text=f"A precise sense of {term}.")],
        source_document_ids=["doc-A"],
    )


def test_exposition_writes_a_linked_conversation_record(mongo_db) -> None:
    """The prose is unusable as evidence if you cannot read how it was made."""
    OntologyEntryRepository(mongo_db).insert(_entry())
    written = enrich_definition_with_detail(
        mongo_db,
        "MPL-950",
        adapter=MockAdapter(default_response=_LONG),
        definition_index=0,
        source_document_id="doc-A",
    )
    assert written

    definition = OntologyEntryRepository(mongo_db).get("MPL-950").definitions[0]
    log_id = definition.detailed_decision_log_id
    assert log_id, "exposition must link its own decision log"
    assert definition.detailed_model_used, "must record which model wrote it"
    assert definition.detailed_created_at is not None

    # The debate provenance is NOT overwritten by the expansion's.
    assert definition.detailed_decision_log_id != definition.decision_log_id

    entry_log = mongo_db.decision_log.find_one({"decision_log_id": log_id})
    assert entry_log["outcome"] == "elaborated"
    assert entry_log["source_document_id"] == "doc-A"
    exchange = mongo_db.agent_exchanges.find_one({"decision_log_id": log_id})
    assert exchange["prompt"] and exchange["response"], "full exchange stored"
    assert "relational substrate" in exchange["prompt"].lower()


def test_expositions_do_not_distort_debate_statistics(mongo_db) -> None:
    """`elaborated` rows share the collection but are not debates."""
    from mahalath.analysis import _debate_stats

    OntologyEntryRepository(mongo_db).insert(_entry())
    mongo_db.decision_log.insert_one(
        {"decision_log_id": "d1", "term": "t", "source_document_id": "doc-A",
         "outcome": "accepted", "iterations_used": 2, "final_confidence": 9.0}
    )
    enrich_definition_with_detail(
        mongo_db, "MPL-950", adapter=MockAdapter(default_response=_LONG),
        definition_index=0,
    )
    stats = _debate_stats(mongo_db)
    assert stats.total == 1, "the exposition must not be counted as a debate"
    assert "elaborated" not in stats.by_outcome


def test_prose_is_not_stored_when_the_audit_write_fails(mongo_db) -> None:
    """Better no exposition than one with no record of where it came from."""
    OntologyEntryRepository(mongo_db).insert(_entry())
    import mahalath.detailed as detailed_mod

    original = detailed_mod.record_detailed_audit
    detailed_mod.record_detailed_audit = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("mongo down")
    )
    try:
        with pytest.raises(DetailedError):
            enrich_definition_with_detail(
                mongo_db, "MPL-950",
                adapter=MockAdapter(default_response=_LONG),
                definition_index=0,
            )
    finally:
        detailed_mod.record_detailed_audit = original

    definition = OntologyEntryRepository(mongo_db).get("MPL-950").definitions[0]
    assert definition.detailed_text is None


def test_failure_reports_the_real_reason_not_a_fixed_string(mongo_db) -> None:
    """The adapter usually names the host and the remedy; keep that."""
    from mahalath.adapters.base import Adapter, AdapterError

    class Unreachable(Adapter):
        def generate(self, prompt, model=None, want_json=False):
            raise AdapterError("could not connect to http://x:11434 (errno 111)")

    OntologyEntryRepository(mongo_db).insert(_entry())
    result = backfill_detailed_definitions(
        AppConfig(runtime=RuntimeConfig()), mongo_db, Unreachable(),
        max_items=5, apply=True,
    )
    assert result.errored == 1
    assert "errno 111" in result.errors[0]
    assert "generation failed" not in result.errors[0]


def test_max_items_bounds_attempts_not_successes(mongo_db) -> None:
    """A failing model must stop at the limit, not walk the collection."""
    from mahalath.adapters.base import Adapter, AdapterError

    class Counting(Adapter):
        def __init__(self) -> None:
            self.calls = 0

        def generate(self, prompt, model=None, want_json=False):
            self.calls += 1
            raise AdapterError("model unavailable")

    repo = OntologyEntryRepository(mongo_db)
    for i in range(1, 11):
        repo.insert(_entry(label=f"MPL-9{i:02d}", term=f"term {i}"))
    adapter = Counting()
    result = backfill_detailed_definitions(
        AppConfig(runtime=RuntimeConfig()), mongo_db, adapter,
        max_items=3, apply=True,
    )
    assert adapter.calls == 3, f"asked for 3, made {adapter.calls} calls"
    assert result.attempted == 3
    assert result.written == 0


def test_source_snippet_prefers_the_triggering_document(mongo_db, tmp_path) -> None:
    """A multi-source entry must not silently quote the wrong document."""
    from mahalath.detailed import source_snippet_for_entry

    a = tmp_path / "a.md"
    a.write_text("Document A discusses the widget at length.", encoding="utf-8")
    b = tmp_path / "b.md"
    b.write_text("Document B mentions the widget differently.", encoding="utf-8")
    mongo_db.documents.insert_many([
        {"document_id": "doc-A", "archive_path": str(a), "checksum_sha256": "aa"},
        {"document_id": "doc-B", "archive_path": str(b), "checksum_sha256": "bb"},
    ])
    entry = OntologyEntry(
        mpl_label="MPL-960", canonical_term="widget", confidence=8.0,
        definitions=[DefinitionVersion(text="A widget.")],
        source_document_ids=["doc-A", "doc-B"],
    )
    assert "Document A" in source_snippet_for_entry(mongo_db, entry)
    assert "Document B" in source_snippet_for_entry(
        mongo_db, entry, source_document_id="doc-B"
    )
