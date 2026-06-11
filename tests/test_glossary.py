"""Glossary export tests against a live MongoDB test db."""

from __future__ import annotations

import json
from pathlib import Path

from mahalath.db.models import (
    DefinitionContext,
    DefinitionVersion,
    OntologyEntry,
    OntologyTreeEdge,
)
from mahalath.db.repositories import (
    DefinitionContextRepository,
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


def _seed_with_contexts(mongo_db):
    """One entry holding definitions in two frames + one untagged."""
    ctx_repo = DefinitionContextRepository(mongo_db)
    ctx_st = DefinitionContext(name="structural", description="Structural frame.")
    ctx_th = DefinitionContext(name="theological", description="Theological frame.")
    ctx_repo.insert(ctx_st)
    ctx_repo.insert(ctx_th)
    OntologyEntryRepository(mongo_db).insert(OntologyEntry(
        mpl_label="MPL-001", canonical_term="substrate", confidence=8.0,
        definitions=[
            DefinitionVersion(text="The structural meaning.",
                              model_used="gemma4:e2b",
                              context_id=ctx_st.context_id),
            DefinitionVersion(text="The theological meaning.",
                              model_used="operator",
                              context_id=ctx_th.context_id),
            DefinitionVersion(text="A legacy untagged meaning."),
        ],
    ))
    return ctx_st, ctx_th


def test_markdown_export_groups_definitions_by_frame(mongo_db) -> None:
    _seed_with_contexts(mongo_db)
    body = export_markdown(mongo_db, database_name="x").output
    assert "**Frame:** _structural_" in body
    assert "**Frame:** _theological_" in body
    # Untagged definitions collect into their own group, rendered last.
    assert "**Frame:** _(untagged)_" in body
    assert body.index("_(untagged)_") > body.index("_theological_")
    assert "> The structural meaning." in body


def test_markdown_export_frameless_entry_renders_flat(mongo_db) -> None:
    # No definition carries a context → no Frame headings (Stage 1 shape).
    _seed(mongo_db)
    body = export_markdown(mongo_db, database_name="x").output
    assert "**Frame:**" not in body
    assert "> The fundamental underlying medium." in body


def test_json_export_carries_definition_contexts(mongo_db) -> None:
    ctx_st, ctx_th = _seed_with_contexts(mongo_db)
    payload = json.loads(export_json(mongo_db, database_name="x").output)
    defs = payload["entries"][0]["definitions"]
    assert defs[0]["context_id"] == ctx_st.context_id
    assert defs[0]["context_name"] == "structural"
    assert defs[1]["context_id"] == ctx_th.context_id
    assert defs[1]["context_name"] == "theological"
    assert defs[2]["context_id"] is None
    assert defs[2]["context_name"] is None


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
