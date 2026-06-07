"""refresh_glossary writes both files; idempotent on no-change."""

from __future__ import annotations

import json
from pathlib import Path

from mahalath.config import AppConfig, MongoConfig, PathConfig
from mahalath.db.models import (
    DefinitionVersion,
    OntologyEntry,
)
from mahalath.db.repositories import OntologyEntryRepository
from mahalath.glossary import refresh_glossary


def _config(tmp_path: Path, db_name: str) -> AppConfig:
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


def _seed_one(mongo_db) -> None:
    OntologyEntryRepository(mongo_db).insert(OntologyEntry(
        mpl_label="MPL-001", canonical_term="agonist", confidence=8.5,
        definitions=[DefinitionVersion(
            text="An agonist activates a receptor.",
            model_used="gemma4:e2b",
        )],
    ))


def test_refresh_glossary_writes_both_files(
    tmp_path: Path, mongo_db, mongo_config: AppConfig
) -> None:
    _seed_one(mongo_db)
    config = _config(tmp_path, mongo_config.mongo.database)

    results = refresh_glossary(config, mongo_db, project_root=tmp_path)
    assert set(results) == {"md", "json"}

    md_path = tmp_path / "ontology" / "glossary.md"
    json_path = tmp_path / "ontology" / "glossary.json"
    assert md_path.exists()
    assert json_path.exists()

    assert "MPL-001 — agonist" in md_path.read_text(encoding="utf-8")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["count"] == 1
    assert payload["entries"][0]["canonical_term"] == "agonist"


def test_refresh_glossary_creates_missing_output_directory(
    tmp_path: Path, mongo_db, mongo_config: AppConfig
) -> None:
    _seed_one(mongo_db)
    config = _config(tmp_path / "nested" / "deeper", mongo_config.mongo.database)
    refresh_glossary(config, mongo_db, project_root=tmp_path)
    # Path config points the ontology output into a deeply nested location.
    expected = tmp_path / "nested" / "deeper" / "ontology" / "glossary.md"
    assert expected.exists()


def test_refresh_glossary_idempotent(
    tmp_path: Path, mongo_db, mongo_config: AppConfig
) -> None:
    _seed_one(mongo_db)
    config = _config(tmp_path, mongo_config.mongo.database)
    refresh_glossary(config, mongo_db, project_root=tmp_path)
    md_path = tmp_path / "ontology" / "glossary.md"
    json_path = tmp_path / "ontology" / "glossary.json"

    first_md_bytes = md_path.read_bytes()
    first_json = json.loads(json_path.read_text(encoding="utf-8"))

    refresh_glossary(config, mongo_db, project_root=tmp_path)
    second_md_bytes = md_path.read_bytes()
    second_json = json.loads(json_path.read_text(encoding="utf-8"))

    # Markdown header carries a generated_at timestamp so byte-equality
    # is not expected, but the count and entry list should match.
    assert b"MPL-001" in second_md_bytes
    assert first_json["count"] == second_json["count"]
    assert first_json["entries"][0]["mpl_label"] == second_json["entries"][0]["mpl_label"]


def test_refresh_glossary_rejects_unknown_format(
    tmp_path: Path, mongo_db, mongo_config: AppConfig
) -> None:
    import pytest
    _seed_one(mongo_db)
    config = _config(tmp_path, mongo_config.mongo.database)
    with pytest.raises(ValueError):
        refresh_glossary(
            config, mongo_db, formats=("xml",), project_root=tmp_path
        )
