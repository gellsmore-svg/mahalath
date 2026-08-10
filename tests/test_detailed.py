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
