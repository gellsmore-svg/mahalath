"""process-document pipeline: post-persist context backfill (S2.27).

The debate already proposes a context per definition, but the model
sometimes omits it. These tests drive `_run_pipeline_on_document` with a
keyed MockAdapter where the SynthesisExplorer returns no context_name,
so the persisted definition lands untagged — and assert the scoped
backfill pass tags it (and that the skip flag suppresses it).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from mahalath.adapters import MockAdapter
from mahalath.cli import _run_pipeline_on_document
from mahalath.config import AppConfig, MongoConfig, PathConfig
from mahalath.db.models import DefinitionContext, DocumentRecord
from mahalath.db.repositories import (
    DefinitionContextRepository,
    DocumentRepository,
    OntologyEntryRepository,
)
from mahalath.debate import (
    SPEAKER_TAG_PRECISION_CRITIC,
    SPEAKER_TAG_SYNTHESIS_EXPLORER,
)

ARCHIVE_TEXT = "# Pharmacology\n\nAn agonist activates the receptor.\n"


def _config(tmp_path: Path, db_name: str) -> AppConfig:
    cfg = AppConfig(
        mongo=MongoConfig(database=db_name),
        paths=PathConfig(
            input=tmp_path / "input",
            processed=tmp_path / "processed",
            ontology=tmp_path / "ontology",
            logs=tmp_path / "logs",
            undecided=tmp_path / "undecided",
        ),
    )
    for p in (cfg.paths.processed, cfg.paths.ontology, cfg.paths.logs):
        Path(p).mkdir(parents=True, exist_ok=True)
    return cfg


def _seed_document(tmp_path: Path, db) -> DocumentRecord:
    archive = tmp_path / "processed" / "pharma.md"
    archive.write_text(ARCHIVE_TEXT, encoding="utf-8")
    doc = DocumentRecord(
        source_path=str(tmp_path / "input" / "pharma.md"),
        archive_path=str(archive),  # absolute → used as-is by the pipeline
        checksum_sha256=hashlib.sha256(ARCHIVE_TEXT.encode()).hexdigest(),
        title="Pharmacology",
        byte_size=len(ARCHIVE_TEXT.encode()),
        char_count=len(ARCHIVE_TEXT),
    )
    DocumentRepository(db).insert(doc)
    return doc


def _adapter() -> MockAdapter:
    # default_response feeds the extraction call (no speaker tag / backfill
    # needle in that prompt). The SE response deliberately omits
    # context_name so the persisted definition is untagged.
    return MockAdapter(
        default_response=json.dumps(
            {"candidates": [{"term": "agonist", "context": "activates a receptor"}]}
        ),
        responses={
            SPEAKER_TAG_PRECISION_CRITIC: json.dumps(
                {"definition": "An agonist activates a receptor.",
                 "critique": "ok", "confidence": 9.0}
            ),
            SPEAKER_TAG_SYNTHESIS_EXPLORER: json.dumps(
                {"definition": "An agonist activates a receptor.",
                 "rationale": "ok", "confidence": 8.5}
            ),
            "tagging an existing ontology definition": json.dumps(
                {"context_name": "structural", "rationale": "mechanism frame"}
            ),
        },
    )


def test_pipeline_backfill_tags_definition_debate_left_untagged(
    tmp_path: Path, mongo_config, mongo_db
) -> None:
    cfg = _config(tmp_path, mongo_config.mongo.database)
    DefinitionContextRepository(mongo_db).insert(
        DefinitionContext(name="structural", description="Mechanism / structural frame.")
    )
    doc = _seed_document(tmp_path, mongo_db)

    result = _run_pipeline_on_document(
        doc, mongo_db, _adapter(), cfg,
        max_terms=1,
        skip_hierarchy_review=True,
        consensus_passes_override=None,
        style_overlay=None,
    )

    assert result["ok"] is True
    # The debate accepted one entry, and the backfill tagged it.
    accepted = [d for d in result["debated"] if d["outcome"] == "accepted"]
    assert len(accepted) == 1
    cb = result["context_backfill"]
    assert cb is not None
    assert cb["applied"] == 1
    assert cb["proposals"][0]["context"] == "structural"

    # The persisted definition now carries the structural context_id.
    label = accepted[0]["mpl_label"]
    ctx = DefinitionContextRepository(mongo_db).get_by_name("structural")
    stored = OntologyEntryRepository(mongo_db).get(label)
    assert stored.definitions[0].context_id == ctx.context_id


def test_pipeline_skip_flag_leaves_definition_untagged(
    tmp_path: Path, mongo_config, mongo_db
) -> None:
    cfg = _config(tmp_path, mongo_config.mongo.database)
    DefinitionContextRepository(mongo_db).insert(
        DefinitionContext(name="structural", description="Mechanism / structural frame.")
    )
    doc = _seed_document(tmp_path, mongo_db)

    result = _run_pipeline_on_document(
        doc, mongo_db, _adapter(), cfg,
        max_terms=1,
        skip_hierarchy_review=True,
        consensus_passes_override=None,
        style_overlay=None,
        skip_context_backfill=True,
    )

    assert result["context_backfill"] is None
    label = [d for d in result["debated"] if d["outcome"] == "accepted"][0]["mpl_label"]
    stored = OntologyEntryRepository(mongo_db).get(label)
    assert stored.definitions[0].context_id is None


# --- I-B: post-persist intent attribution ------------------------------------


def _adapter_with_intents() -> MockAdapter:
    from mahalath.intents import INTENT_ATTRIBUTION_TAG

    adapter = _adapter()
    # Same verdict every pass -> unanimity trivially satisfied (3 passes).
    adapter.responses[INTENT_ATTRIBUTION_TAG] = json.dumps({
        "intent_tags": ["teach"],
        "intentionality": "high",
        "confidence": 9.0,
    })
    return adapter


def test_pipeline_attributes_intent_on_accepted_entry(
    tmp_path: Path, mongo_config, mongo_db
) -> None:
    from mahalath.intents import resolve_intent_tag, seed_intents

    cfg = _config(tmp_path, mongo_config.mongo.database)
    DefinitionContextRepository(mongo_db).insert(
        DefinitionContext(name="structural", description="Mechanism frame.")
    )
    seed_intents(mongo_db)
    doc = _seed_document(tmp_path, mongo_db)

    result = _run_pipeline_on_document(
        doc, mongo_db, _adapter_with_intents(), cfg,
        max_terms=1,
        skip_hierarchy_review=True,
        consensus_passes_override=None,
        style_overlay=None,
    )

    ib = result["intent_backfill"]
    assert ib is not None
    assert ib["stored"] == 1
    assert ib["attributions"][0]["tags"] == ["teach"]
    assert ib["attributions"][0]["intentionality"] == "high"

    label = [d for d in result["debated"] if d["outcome"] == "accepted"][0]["mpl_label"]
    stored = OntologyEntryRepository(mongo_db).get(label)
    teach_id = resolve_intent_tag(mongo_db, "teach")
    assert stored.definitions[0].intent_tags == [teach_id]
    assert stored.definitions[0].intentionality == "high"
    assert stored.definitions[0].intent_confidence == 9.0


def test_pipeline_intent_skip_flag(tmp_path: Path, mongo_config, mongo_db) -> None:
    from mahalath.intents import seed_intents

    cfg = _config(tmp_path, mongo_config.mongo.database)
    DefinitionContextRepository(mongo_db).insert(
        DefinitionContext(name="structural", description="Mechanism frame.")
    )
    seed_intents(mongo_db)
    doc = _seed_document(tmp_path, mongo_db)

    result = _run_pipeline_on_document(
        doc, mongo_db, _adapter_with_intents(), cfg,
        max_terms=1,
        skip_hierarchy_review=True,
        consensus_passes_override=None,
        style_overlay=None,
        skip_intent_backfill=True,
    )

    assert result["intent_backfill"] is None
    label = [d for d in result["debated"] if d["outcome"] == "accepted"][0]["mpl_label"]
    stored = OntologyEntryRepository(mongo_db).get(label)
    assert stored.definitions[0].intent_tags == []
    assert stored.definitions[0].intentionality is None


def test_pipeline_intent_noop_without_taxonomy(
    tmp_path: Path, mongo_config, mongo_db
) -> None:
    # No intent rows defined -> the tail no-ops without consulting the
    # model (the keyed response is absent; an intent prompt would have
    # fallen through to default_response and parse-failed loudly).
    cfg = _config(tmp_path, mongo_config.mongo.database)
    DefinitionContextRepository(mongo_db).insert(
        DefinitionContext(name="structural", description="Mechanism frame.")
    )
    doc = _seed_document(tmp_path, mongo_db)

    result = _run_pipeline_on_document(
        doc, mongo_db, _adapter(), cfg,
        max_terms=1,
        skip_hierarchy_review=True,
        consensus_passes_override=None,
        style_overlay=None,
    )

    ib = result["intent_backfill"]
    assert ib is not None and ib["attempted"] == 0 and ib["stored"] == 0
