"""Ingestion tests against a live MongoDB test database.

Covers: clean ingest, duplicate detection, title extraction from first
heading, archive copy + checksum verification, activity log emission.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from mahalath.config import AppConfig, MongoConfig, PathConfig
from mahalath.db.repositories import DocumentRepository
from mahalath.ingestion import IngestionError, ingest_one


def _config_for(tmp_path: Path, db_name: str) -> AppConfig:
    return AppConfig(
        mongo=MongoConfig(database=db_name),
        paths=PathConfig(
            input=tmp_path / "input",
            processed=tmp_path / "processed",
            ontology=tmp_path / "ontology",
            logs=tmp_path / "logs",
            undecided=tmp_path / "undecided",
        ),
    )


def test_ingest_one_writes_document_and_archive(
    tmp_path: Path, mongo_db, mongo_config: AppConfig
) -> None:
    config = _config_for(tmp_path, mongo_config.mongo.database)
    src = tmp_path / "input" / "sample.md"
    src.parent.mkdir(parents=True)
    src.write_text("# Receptor pharmacology\n\nAn agonist activates the receptor.\n", encoding="utf-8")

    result = ingest_one(src, config, mongo_db, project_root=tmp_path)

    assert result.duplicate is False
    assert result.document.title == "Receptor pharmacology"
    assert result.document.byte_size > 0
    assert result.document.checksum_sha256 == hashlib.sha256(src.read_bytes()).hexdigest()

    # Archive copy exists with matching content.
    archive = tmp_path / result.document.archive_path
    assert archive.exists()
    assert archive.read_bytes() == src.read_bytes()

    # Activity log emitted with the document_id in its name.
    assert result.activity_log_path is not None
    assert result.document.document_id in str(result.activity_log_path)
    log_text = result.activity_log_path.read_text(encoding="utf-8")
    assert "Receptor pharmacology" in log_text
    assert result.document.document_id in log_text


def test_ingest_one_rejects_duplicate_by_checksum(
    tmp_path: Path, mongo_db, mongo_config: AppConfig
) -> None:
    config = _config_for(tmp_path, mongo_config.mongo.database)
    src1 = tmp_path / "input" / "first.md"
    src2 = tmp_path / "input" / "renamed-copy.md"
    src1.parent.mkdir(parents=True)
    content = "# A document\n\nIdentical bytes.\n"
    src1.write_text(content, encoding="utf-8")
    src2.write_text(content, encoding="utf-8")

    first = ingest_one(src1, config, mongo_db, project_root=tmp_path)
    second = ingest_one(src2, config, mongo_db, project_root=tmp_path)

    assert first.duplicate is False
    assert second.duplicate is True
    # The "existing" record returned is the first one.
    assert second.document.document_id == first.document.document_id

    # Documents collection still has exactly one record.
    assert DocumentRepository(mongo_db).count() == 1


def test_ingest_one_falls_back_to_filename_when_no_heading(
    tmp_path: Path, mongo_db, mongo_config: AppConfig
) -> None:
    config = _config_for(tmp_path, mongo_config.mongo.database)
    src = tmp_path / "input" / "untitled-thoughts.md"
    src.parent.mkdir(parents=True)
    src.write_text("no heading here, just prose.\n", encoding="utf-8")

    result = ingest_one(src, config, mongo_db, project_root=tmp_path)
    assert result.document.title == "untitled-thoughts"


def test_ingest_one_raises_when_source_missing(
    tmp_path: Path, mongo_db, mongo_config: AppConfig
) -> None:
    config = _config_for(tmp_path, mongo_config.mongo.database)
    with pytest.raises(IngestionError):
        ingest_one(tmp_path / "nope.md", config, mongo_db, project_root=tmp_path)


def test_ingest_one_stores_style_overlay_path(
    tmp_path: Path, mongo_db, mongo_config: AppConfig
) -> None:
    config = _config_for(tmp_path, mongo_config.mongo.database)
    src = tmp_path / "input" / "doc.md"
    src.parent.mkdir(parents=True)
    src.write_text("# Doc\n\nBody.\n", encoding="utf-8")

    result = ingest_one(
        src, config, mongo_db,
        project_root=tmp_path,
        style_overlay_path="docs/voice.md",
    )
    assert result.document.style_overlay_path == "docs/voice.md"

    # Round-trip via the DB confirms the field is persisted.
    from mahalath.db.repositories import DocumentRepository
    fetched = DocumentRepository(mongo_db).find_by_document_id(
        result.document.document_id
    )
    assert fetched is not None
    assert fetched.style_overlay_path == "docs/voice.md"


def test_ingest_one_disambiguates_archive_name_collision(
    tmp_path: Path, mongo_db, mongo_config: AppConfig
) -> None:
    config = _config_for(tmp_path, mongo_config.mongo.database)
    src = tmp_path / "input" / "notes.md"
    src.parent.mkdir(parents=True)
    src.write_text("# Notes\n\nReal content one.\n", encoding="utf-8")

    # Pre-existing file in processed/ with the same target name.
    processed = tmp_path / "processed"
    processed.mkdir()
    (processed / "notes.md").write_text("unrelated previous file", encoding="utf-8")

    result = ingest_one(src, config, mongo_db, project_root=tmp_path)
    archive = tmp_path / result.document.archive_path

    assert archive.exists()
    # The pre-existing file is untouched and still has its original content.
    assert (processed / "notes.md").read_text(encoding="utf-8") == "unrelated previous file"
    # The archived copy carries the checksum suffix.
    assert archive.name != "notes.md"
    assert archive.name.startswith("notes__")


def test_ingest_one_stores_language(
    tmp_path: Path, mongo_db, mongo_config: AppConfig
) -> None:
    config = _config_for(tmp_path, mongo_config.mongo.database)
    src = tmp_path / "input" / "beispiel.md"
    src.parent.mkdir(parents=True)
    src.write_text("# Beispiel\n\nEin Gewebe ist ein Geflecht.\n", encoding="utf-8")

    result = ingest_one(src, config, mongo_db, project_root=tmp_path,
                        language="de")
    assert result.document.language == "de"
    stored = mongo_db.documents.find_one({"document_id": result.document.document_id})
    assert stored["language"] == "de"
