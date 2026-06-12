"""Pydantic model smoke tests.

Verifies default values, schema_version, and that obviously-wrong shapes
are rejected. DB round-tripping is exercised in integration tests where
a Mongo instance is available.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from mahalath.db.models import (
    SCHEMA_VERSION,
    AgentExchange,
    DecisionLogEntry,
    DocumentRecord,
    OntologyEntry,
    OntologyTreeEdge,
    UndecidedItem,
)


def test_document_record_defaults() -> None:
    rec = DocumentRecord(
        source_path="input/foo.md",
        archive_path="processed/foo.md",
        checksum_sha256="0" * 64,
        byte_size=1024,
        char_count=512,
    )
    UUID(rec.document_id)
    assert rec.processed_at is None
    assert isinstance(rec.ingested_at, datetime)
    assert rec.schema_version == SCHEMA_VERSION


def test_document_record_ignores_extra_id_field() -> None:
    rec = DocumentRecord.model_validate(
        {
            "_id": "ignored-objectid",
            "source_path": "x",
            "archive_path": "y",
            "checksum_sha256": "0" * 64,
            "byte_size": 1,
            "char_count": 1,
        }
    )
    assert rec.source_path == "x"


def test_ontology_entry_required_label_and_confidence() -> None:
    entry = OntologyEntry(
        mpl_label="MPL-001",
        canonical_term="agonist",
        confidence=8.4,
    )
    assert entry.status == "accepted"
    assert entry.parent_label is None
    assert entry.definitions == []
    assert entry.aliases == []
    assert entry.schema_version == SCHEMA_VERSION


def test_ontology_entry_rejects_missing_confidence() -> None:
    with pytest.raises(ValidationError):
        OntologyEntry(mpl_label="MPL-001", canonical_term="agonist")  # type: ignore[call-arg]


def test_ontology_tree_edge_defaults() -> None:
    edge = OntologyTreeEdge(parent_label="MPL-001", child_label="MPL-001.002")
    assert edge.relation_type == "child_of"
    assert edge.schema_version == SCHEMA_VERSION


def test_decision_log_entry_default_outcome_is_undecided() -> None:
    entry = DecisionLogEntry(term="agonist", source_document_id="doc-1")
    UUID(entry.decision_log_id)
    assert entry.outcome == "undecided"
    assert entry.iterations_used == 0
    assert entry.messages == []
    assert entry.resulting_mpl_labels == []


def test_agent_exchange_records_iteration_and_role() -> None:
    ex = AgentExchange(
        decision_log_id="dl-1",
        iteration=3,
        role="precision_critic",
        model="gemma3:1b",
        prompt="...",
        response="...",
        confidence=7.5,
    )
    assert ex.iteration == 3
    assert ex.role == "precision_critic"


def test_undecided_item_requires_reason() -> None:
    with pytest.raises(ValidationError):
        UndecidedItem(  # type: ignore[call-arg]
            decision_log_id="dl-1",
            term="foo",
            source_document_id="doc-1",
        )
    item = UndecidedItem(
        decision_log_id="dl-1",
        term="foo",
        source_document_id="doc-1",
        reason="below_threshold",
    )
    assert item.escalation_level == 0
    assert item.last_confidence is None


# --- text-index override migration (M-A) --------------------------------------


def test_text_index_uses_decoupled_override(mongo_db) -> None:
    info = mongo_db.ontology_entries.index_information()
    assert info["ontology_text"]["language_override"] == "text_language"


def test_legacy_text_index_is_migrated(mongo_db) -> None:
    from pymongo import TEXT
    from mahalath.db import ensure_indexes

    # Recreate the pre-M-A index shape (default override = "language").
    mongo_db.ontology_entries.drop_index("ontology_text")
    mongo_db.ontology_entries.create_index(
        [("canonical_term", TEXT), ("aliases", TEXT), ("definitions.text", TEXT)],
        weights={"canonical_term": 10, "aliases": 5, "definitions.text": 1},
        name="ontology_text",
    )
    ensure_indexes(mongo_db)
    info = mongo_db.ontology_entries.index_information()
    assert info["ontology_text"]["language_override"] == "text_language"

    # An entry whose semantic language Mongo can't stem must insert fine.
    mongo_db.ontology_entries.insert_one({
        "_id": "MPL-901", "mpl_label": "MPL-901", "canonical_term": "样本",
        "language": "zh", "confidence": 8.0, "definitions": [],
    })
    assert mongo_db.ontology_entries.find_one({"_id": "MPL-901"}) is not None


def test_backfill_language_stamps_legacy_records(mongo_db) -> None:
    from mahalath.ontology import backfill_language

    # Raw legacy docs without the language field.
    mongo_db.ontology_entries.insert_one({
        "_id": "MPL-902", "mpl_label": "MPL-902", "canonical_term": "old",
        "confidence": 8.0, "definitions": [],
    })
    mongo_db.documents.insert_one({
        "document_id": "doc-legacy", "source_path": "x", "archive_path": "x",
        "checksum_sha256": "d" * 64, "byte_size": 1, "char_count": 1,
    })
    result = backfill_language(mongo_db)
    assert result == {"entries_stamped": 1, "documents_stamped": 1}
    assert mongo_db.ontology_entries.find_one({"_id": "MPL-902"})["language"] == "en"
    assert mongo_db.documents.find_one({"document_id": "doc-legacy"})["language"] == "en"
    # Idempotent.
    assert backfill_language(mongo_db) == {
        "entries_stamped": 0, "documents_stamped": 0,
    }


def test_insert_stamps_stem_hint_only_for_supported_languages(mongo_db) -> None:
    from mahalath.db.models import OntologyEntry
    from mahalath.db.repositories import OntologyEntryRepository

    repo = OntologyEntryRepository(mongo_db)
    repo.insert(OntologyEntry(mpl_label="MPL-911", canonical_term="Gewebe",
                              confidence=8.0, language="de"))
    repo.insert(OntologyEntry(mpl_label="MPL-912", canonical_term="weave",
                              confidence=8.0, language="en"))
    repo.insert(OntologyEntry(mpl_label="MPL-913", canonical_term="样本",
                              confidence=8.0, language="zh"))
    assert mongo_db.ontology_entries.find_one({"_id": "MPL-911"})["text_language"] == "german"
    # en uses the index default; zh has no stemmer — neither gets the field.
    assert "text_language" not in mongo_db.ontology_entries.find_one({"_id": "MPL-912"})
    assert "text_language" not in mongo_db.ontology_entries.find_one({"_id": "MPL-913"})
