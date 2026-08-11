"""Reading back the conversations behind a term's prose (ADR-034)."""

from __future__ import annotations

from mahalath.adapters import MockAdapter
from mahalath.db.models import DefinitionVersion, OntologyEntry
from mahalath.db.repositories import OntologyEntryRepository
from mahalath.detailed import enrich_definition_with_detail
from mahalath.transcripts import (
    LAYER_DEBATE,
    LAYER_EXPOSITION,
    conversations_for_entry,
    load_conversation,
    render_conversation,
)

_LONG = (
    "The widget is the smallest unit of work in this corpus. It is not a "
    "component in the mechanical sense; it names the unit that the process "
    "actually schedules, which is why the distinction matters here."
)


def _seed(db, *, debate_log: str = "log-debate") -> None:
    db.decision_log.insert_one({
        "decision_log_id": debate_log, "term": "widget",
        "source_document_id": "doc-A", "outcome": "accepted",
        "iterations_used": 2, "final_confidence": 8.5, "messages": [],
    })
    db.agent_exchanges.insert_many([
        {"decision_log_id": debate_log, "iteration": 1, "role": "precision_critic",
         "model": "m1", "prompt": "P1", "response": "R1", "confidence": 8.0},
        {"decision_log_id": debate_log, "iteration": 2, "role": "synthesis_explorer",
         "model": "m2", "prompt": "P2", "response": "R2", "confidence": 8.5},
    ])
    OntologyEntryRepository(db).insert(OntologyEntry(
        mpl_label="MPL-970", canonical_term="widget", confidence=8.5,
        decision_log_id=debate_log,
        definitions=[DefinitionVersion(text="A widget.", decision_log_id=debate_log)],
        source_document_ids=["doc-A"],
    ))


def test_entry_gathers_debate_and_exposition_conversations(mongo_db) -> None:
    _seed(mongo_db)
    enrich_definition_with_detail(
        mongo_db, "MPL-970", adapter=MockAdapter(default_response=_LONG),
        definition_index=0,
    )
    layers = [c.layer for c in conversations_for_entry(mongo_db, "MPL-970")]
    assert LAYER_DEBATE in layers
    assert LAYER_EXPOSITION in layers, "the expansion must be readable too"


def test_conversation_carries_the_full_exchange(mongo_db) -> None:
    _seed(mongo_db)
    conversation = load_conversation(mongo_db, "log-debate")
    assert [e.role for e in conversation.exchanges] == [
        "precision_critic", "synthesis_explorer"
    ]
    rendered = render_conversation(conversation)
    assert "R1" in rendered and "R2" in rendered
    assert "P1" not in rendered, "prompts are verbose-only by default"
    assert "P1" in render_conversation(conversation, verbose=True)


def test_prose_predating_capture_is_reported_not_hidden(mongo_db) -> None:
    """An entry pointing at a log that no longer exists must say so."""
    OntologyEntryRepository(mongo_db).insert(OntologyEntry(
        mpl_label="MPL-971", canonical_term="legacy", confidence=8.0,
        decision_log_id="vanished",
        definitions=[DefinitionVersion(text="A legacy term.")],
        source_document_ids=["doc-A"],
    ))
    [conversation] = conversations_for_entry(mongo_db, "MPL-971")
    assert conversation.missing is True
    assert "no record" in render_conversation(conversation)


def test_unknown_entry_returns_nothing(mongo_db) -> None:
    assert conversations_for_entry(mongo_db, "MPL-999") == []
    assert load_conversation(mongo_db, "nope") is None
