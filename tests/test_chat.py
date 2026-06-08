"""Chat backend tests with MockAdapter."""

from __future__ import annotations

import pytest

from mahalath.adapters import MockAdapter
from mahalath.chat import (
    answer_question,
    build_chat_prompt,
    extract_cited_labels,
    select_context_entries,
)
from mahalath.db.models import DefinitionVersion, OntologyEntry
from mahalath.db.repositories import OntologyEntryRepository


def _seed(mongo_db, label, term, *, defs=None, aliases=None, refs=None,
          parent=None, stale=False) -> OntologyEntry:
    entry = OntologyEntry(
        mpl_label=label,
        canonical_term=term,
        confidence=8.0,
        aliases=aliases or [],
        parent_label=parent,
        references_labels=refs or [],
        is_stale=stale,
        definitions=[DefinitionVersion(text=t) for t in (defs or ["x"])],
    )
    OntologyEntryRepository(mongo_db).insert(entry)
    return entry


# --- Context selection ---------------------------------------------------


def test_select_context_matches_canonical_term(mongo_db) -> None:
    _seed(mongo_db, "MPL-001", "Relational Substrate")
    _seed(mongo_db, "MPL-002", "Vortons")
    selected = select_context_entries(
        "How does the Relational Substrate work?", mongo_db, max_entries=5,
    )
    labels = [e.mpl_label for e in selected]
    assert "MPL-001" in labels
    assert "MPL-002" not in labels


def test_select_context_matches_mpl_label_directly(mongo_db) -> None:
    _seed(mongo_db, "MPL-001", "alpha")
    _seed(mongo_db, "MPL-002", "beta")
    selected = select_context_entries("Tell me about MPL-002", mongo_db)
    labels = [e.mpl_label for e in selected]
    assert "MPL-002" in labels


def test_select_context_focus_label_pulls_neighbourhood(mongo_db) -> None:
    _seed(mongo_db, "MPL-001", "alpha")
    _seed(mongo_db, "MPL-002", "beta", refs=["MPL-001"])
    _seed(mongo_db, "MPL-003", "gamma", refs=["MPL-002"])
    selected = select_context_entries(
        "(arbitrary question)", mongo_db,
        focus_label="MPL-002", max_entries=5,
    )
    labels = [e.mpl_label for e in selected]
    assert "MPL-002" in labels  # the focus
    assert "MPL-001" in labels  # referenced by MPL-002
    assert "MPL-003" in labels  # references MPL-002 (reverse)


def test_select_context_short_terms_filtered(mongo_db) -> None:
    _seed(mongo_db, "MPL-001", "T0")  # 2 chars
    selected = select_context_entries("Tell me about T0", mongo_db)
    # T0 is too short for word-boundary semantic match, but MPL-001 is matched
    # by canonical_term? No: 'T0' is < 4 chars → skipped by semantic rule.
    assert [e.mpl_label for e in selected] == []


def test_select_context_aliases_matched(mongo_db) -> None:
    _seed(mongo_db, "MPL-001", "alpha", aliases=["the first one", "primary"])
    selected = select_context_entries("What is primary?", mongo_db)
    labels = [e.mpl_label for e in selected]
    assert "MPL-001" in labels


# --- Prompt construction -------------------------------------------------


def test_build_chat_prompt_inlines_definitions(mongo_db) -> None:
    entry = _seed(
        mongo_db, "MPL-001", "Relational Substrate",
        defs=["The fundamental relational ground.", "Refined relational ground."],
    )
    prompt = build_chat_prompt("What is RS?", [entry])
    assert "MPL-001" in prompt
    assert "Relational Substrate" in prompt
    assert "fundamental relational ground" in prompt
    assert "Refined relational ground" in prompt


def test_build_chat_prompt_notes_stale_state(mongo_db) -> None:
    entry = _seed(mongo_db, "MPL-001", "alpha", stale=True)
    # Inject a stale reason manually for the test
    mongo_db.ontology_entries.update_one(
        {"_id": "MPL-001"},
        {"$push": {"stale_reasons": {
            "change_type": "reparented",
            "note": "parent changed from X to Y",
        }}},
    )
    # Reload
    entry = OntologyEntryRepository(mongo_db).get("MPL-001")
    prompt = build_chat_prompt("?", [entry])
    assert "STALE" in prompt
    assert "reparented" in prompt


# --- Citation extraction -------------------------------------------------


def test_extract_cited_labels() -> None:
    text = "See (MPL-001) and the related (MPL-002.003) plus MPL-001 again."
    assert extract_cited_labels(text) == ["MPL-001", "MPL-002.003"]


# --- End-to-end with MockAdapter -----------------------------------------


def test_answer_question_end_to_end(mongo_db) -> None:
    _seed(mongo_db, "MPL-001", "Relational Substrate",
          defs=["Fundamental relational ground."])
    _seed(mongo_db, "MPL-004", "substrate",
          defs=["The generic underlying medium."])

    adapter = MockAdapter(
        default_response="RS (MPL-001) is a specific kind of substrate (MPL-004)."
    )
    response = answer_question(
        "How are RS and substrate related?", mongo_db, adapter,
    )
    assert "RS" in response.answer
    assert "MPL-001" in response.cited_labels
    assert "MPL-004" in response.cited_labels
    assert response.context_labels  # at least one entry was selected
