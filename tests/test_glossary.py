"""Glossary export tests against a live MongoDB test db."""

from __future__ import annotations

import json
from pathlib import Path

from mahalath.db.models import (
    DefinitionVersion,
    OntologyEntry,
    OntologyTreeEdge,
)
from mahalath.db.repositories import (
    OntologyEntryRepository,
    OntologyTreeRepository,
)
from mahalath.glossary import export_json, export_markdown


def _seed(mongo_db) -> None:
    repo = OntologyEntryRepository(mongo_db)
    tree = OntologyTreeRepository(mongo_db)
    repo.insert(OntologyEntry(
        mpl_label="MPL-001", canonical_term="Substrate", confidence=8.0,
        aliases=["underlying medium", "ground of being"],
        definitions=[DefinitionVersion(
            text="The fundamental underlying medium.",
            model_used="gemma4:e2b",
        )],
    ))
    repo.insert(OntologyEntry(
        mpl_label="MPL-002", canonical_term="Relational Substrate",
        confidence=8.5, parent_label="MPL-001",
        definitions=[DefinitionVersion(
            text="The relational variant of substrate.",
            model_used="gemma4:e2b",
        )],
    ))
    repo.insert(OntologyEntry(
        mpl_label="MPL-003", canonical_term="Continuity", confidence=8.0,
        definitions=[DefinitionVersion(text="No gaps.")],
    ))
    tree.add_edge(OntologyTreeEdge(parent_label="MPL-001", child_label="MPL-002"))


def test_markdown_export_renders_header_and_entries(mongo_db) -> None:
    _seed(mongo_db)
    result = export_markdown(mongo_db, database_name="mahalath_test")
    assert result.format == "md"
    assert result.entry_count == 3
    body = result.output
    assert "Mahalath Ontology Glossary" in body
    assert "mahalath_test" in body
    assert "## MPL-001 — Substrate" in body
    assert "## MPL-002 — Relational Substrate" in body
    assert "## MPL-003 — Continuity" in body
    # Top-level vs parented
    assert "_top-level_" in body
    assert "**Parent:** `MPL-001`" in body
    # Aliases section
    assert "_underlying medium_" in body
    # Children section under MPL-001
    assert "**Children:**" in body
    assert "`MPL-002`" in body


def test_markdown_export_empty_ontology(mongo_db) -> None:
    result = export_markdown(mongo_db, database_name="empty")
    assert result.entry_count == 0
    assert "_(ontology is empty)_" in result.output


def test_markdown_export_writes_file(mongo_db, tmp_path: Path) -> None:
    _seed(mongo_db)
    out = tmp_path / "glossary.md"
    result = export_markdown(mongo_db, out_path=out, database_name="x")
    assert result.written_to == out
    assert out.read_text(encoding="utf-8") == result.output
    assert "MPL-001 — Substrate" in out.read_text(encoding="utf-8")


def test_markdown_export_creates_missing_parent_directories(
    mongo_db, tmp_path: Path
) -> None:
    _seed(mongo_db)
    out = tmp_path / "nested" / "deeper" / "glossary.md"
    export_markdown(mongo_db, out_path=out, database_name="x")
    assert out.exists()


def test_json_export_schema_shape(mongo_db) -> None:
    _seed(mongo_db)
    result = export_json(mongo_db, database_name="mahalath_test")
    assert result.format == "json"
    assert result.entry_count == 3
    payload = json.loads(result.output)
    assert payload["schema"] == "mahalath.glossary.v1"
    assert payload["database"] == "mahalath_test"
    assert payload["count"] == 3
    assert len(payload["entries"]) == 3
    labels = [e["mpl_label"] for e in payload["entries"]]
    assert labels == ["MPL-001", "MPL-002", "MPL-003"]
    # MPL-001 has MPL-002 as a child
    mpl_001 = payload["entries"][0]
    assert mpl_001["children"] == ["MPL-002"]
    assert mpl_001["aliases"] == ["underlying medium", "ground of being"]
    # Definitions carry attribution
    assert mpl_001["definitions"][0]["model_used"] == "gemma4:e2b"


def test_json_export_writes_file(mongo_db, tmp_path: Path) -> None:
    _seed(mongo_db)
    out = tmp_path / "glossary.json"
    result = export_json(mongo_db, out_path=out, database_name="x")
    assert result.written_to == out
    parsed = json.loads(out.read_text(encoding="utf-8"))
    assert parsed["count"] == 3


def test_json_export_handles_empty_ontology(mongo_db) -> None:
    result = export_json(mongo_db, database_name="empty")
    payload = json.loads(result.output)
    assert payload["count"] == 0
    assert payload["entries"] == []
