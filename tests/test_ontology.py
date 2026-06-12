"""Ontology persistence tests.

Exercises persist_debate_result with a live MongoDB test database via
the mongo_db fixture. Covers: top-level label assignment, sequential
labelling within a parent, accepted vs undecided routing, decision log
+ exchange persistence, definition version capture.
"""

from __future__ import annotations

from mahalath.config import RuntimeConfig
from mahalath.db.models import AgentExchange, DebateMessage, DefinitionVersion
from mahalath.db.repositories import (
    AgentExchangeRepository,
    DecisionLogRepository,
    OntologyEntryRepository,
    OntologyTreeRepository,
    UndecidedQueueRepository,
)
from mahalath.debate import DebateResult
from mahalath.ontology import ACCEPTED, UNDECIDED, persist_debate_result


def _accepted_result(term: str = "agonist", *, conf: float = 8.6) -> DebateResult:
    return DebateResult(
        decision_log_id="dl-acc-1",
        term=term,
        source_document_id="doc-1",
        outcome=ACCEPTED,
        final_definition=f"Definition of {term}.",
        final_confidence=conf,
        iterations_used=2,
        messages=[
            DebateMessage(iteration=1, role="precision_critic", content="...", confidence=7.0),
            DebateMessage(iteration=1, role="synthesis_explorer", content="...", confidence=8.5),
            DebateMessage(iteration=2, role="precision_critic", content="...", confidence=8.6),
            DebateMessage(iteration=2, role="synthesis_explorer", content="...", confidence=8.8),
        ],
        exchanges=[
            AgentExchange(
                decision_log_id="dl-acc-1", iteration=1, role="precision_critic",
                model="gemma3:1b", prompt="...", response="...", confidence=7.0,
            ),
            AgentExchange(
                decision_log_id="dl-acc-1", iteration=1, role="synthesis_explorer",
                model="gemma3:1b", prompt="...", response="...", confidence=8.5,
            ),
        ],
    )


def _undecided_result(term: str = "murky") -> DebateResult:
    return DebateResult(
        decision_log_id="dl-und-1",
        term=term,
        source_document_id="doc-2",
        outcome=UNDECIDED,
        final_definition="Best-effort definition.",
        final_confidence=6.0,
        iterations_used=25,
        messages=[],
        exchanges=[
            AgentExchange(
                decision_log_id="dl-und-1", iteration=25, role="precision_critic",
                model="gemma3:1b", prompt="...", response="...", confidence=6.0,
            ),
        ],
    )


def test_persist_accepted_assigns_top_level_label(mongo_db) -> None:
    runtime = RuntimeConfig()
    persisted = persist_debate_result(_accepted_result(), mongo_db, runtime)

    assert persisted.outcome == ACCEPTED
    assert persisted.mpl_label == "MPL-001"
    assert persisted.ontology_entry is not None
    assert persisted.ontology_entry.canonical_term == "agonist"
    assert persisted.ontology_entry.parent_label is None
    assert persisted.ontology_entry.confidence == 8.6
    assert len(persisted.ontology_entry.definitions) == 1
    assert persisted.ontology_entry.definitions[0].model_used == "gemma3:1b"

    # Stored in MongoDB.
    fetched = OntologyEntryRepository(mongo_db).get("MPL-001")
    assert fetched is not None
    assert fetched.canonical_term == "agonist"

    # Decision log and exchanges also persisted.
    dl = DecisionLogRepository(mongo_db).get("dl-acc-1")
    assert dl is not None
    assert dl.outcome == ACCEPTED
    assert dl.resulting_mpl_labels == ["MPL-001"]
    assert AgentExchangeRepository(mongo_db).for_decision("dl-acc-1")


def test_persist_accepted_sequential_top_level_labels(mongo_db) -> None:
    runtime = RuntimeConfig()
    r1 = _accepted_result("agonist")
    r1.decision_log_id = "dl-1"
    r1.exchanges = [
        AgentExchange(decision_log_id="dl-1", iteration=1, role="precision_critic",
                      model="gemma3:1b", prompt="...", response="...")
    ]
    persist_debate_result(r1, mongo_db, runtime)

    r2 = _accepted_result("receptor")
    r2.decision_log_id = "dl-2"
    r2.exchanges = [
        AgentExchange(decision_log_id="dl-2", iteration=1, role="precision_critic",
                      model="gemma3:1b", prompt="...", response="...")
    ]
    persisted2 = persist_debate_result(r2, mongo_db, runtime)

    assert persisted2.mpl_label == "MPL-002"
    labels = sorted(OntologyEntryRepository(mongo_db).all_labels())
    assert labels == ["MPL-001", "MPL-002"]


def test_persist_accepted_under_parent_creates_tree_edge(mongo_db) -> None:
    runtime = RuntimeConfig()

    # Parent first.
    parent = _accepted_result("agonist")
    parent.decision_log_id = "dl-p"
    parent.exchanges = [
        AgentExchange(decision_log_id="dl-p", iteration=1, role="precision_critic",
                      model="gemma3:1b", prompt="...", response="..."),
    ]
    p = persist_debate_result(parent, mongo_db, runtime)
    assert p.mpl_label == "MPL-001"

    # Child under it.
    child = _accepted_result("full agonist")
    child.decision_log_id = "dl-c"
    child.exchanges = [
        AgentExchange(decision_log_id="dl-c", iteration=1, role="precision_critic",
                      model="gemma3:1b", prompt="...", response="..."),
    ]
    c = persist_debate_result(child, mongo_db, runtime, parent_label="MPL-001")
    assert c.mpl_label == "MPL-001.001"

    # Tree edge written.
    children = OntologyTreeRepository(mongo_db).children_of("MPL-001")
    assert children == ["MPL-001.001"]
    # Denormalised parent_label on the child entry.
    fetched_child = OntologyEntryRepository(mongo_db).get("MPL-001.001")
    assert fetched_child is not None
    assert fetched_child.parent_label == "MPL-001"


def test_persist_undecided_routes_to_queue_and_writes_decision_log(mongo_db) -> None:
    runtime = RuntimeConfig(max_iterations_per_term=25)
    persisted = persist_debate_result(_undecided_result(), mongo_db, runtime)

    assert persisted.outcome == UNDECIDED
    assert persisted.ontology_entry is None
    assert persisted.undecided_item is not None
    assert persisted.undecided_item.reason == "iteration_cap"
    assert persisted.undecided_item.last_confidence == 6.0

    # No ontology entry was written.
    assert OntologyEntryRepository(mongo_db).all_labels() == []

    # Decision log records the undecided outcome.
    dl = DecisionLogRepository(mongo_db).get("dl-und-1")
    assert dl is not None
    assert dl.outcome == UNDECIDED
    assert dl.resulting_mpl_labels == []

    # Queue has one pending item.
    pending = UndecidedQueueRepository(mongo_db).list_pending()
    assert len(pending) == 1
    assert pending[0].term == "murky"


def test_persist_undecided_with_below_threshold_when_not_at_cap(mongo_db) -> None:
    runtime = RuntimeConfig(max_iterations_per_term=25)
    result = _undecided_result()
    result.iterations_used = 10  # below the cap

    persisted = persist_debate_result(result, mongo_db, runtime)
    assert persisted.undecided_item is not None
    assert persisted.undecided_item.reason == "below_threshold"


# --- lexicon membership (M-A, ADR-028) ----------------------------------------


def test_entry_inherits_document_language(mongo_db) -> None:
    from mahalath.db.models import DocumentRecord
    from mahalath.db.repositories import DocumentRepository

    DocumentRepository(mongo_db).insert(DocumentRecord(
        document_id="doc-de-1", source_path="x.md", archive_path="x.md",
        checksum_sha256="c" * 64, byte_size=1, char_count=1, language="de",
    ))
    result = _accepted_result("Gewebe")
    result.source_document_id = "doc-de-1"
    persisted = persist_debate_result(result, mongo_db, RuntimeConfig())
    entry = OntologyEntryRepository(mongo_db).get(persisted.mpl_label)
    assert entry.language == "de"


def test_entry_defaults_to_english_without_document(mongo_db) -> None:
    # Legacy/synthetic source ids (no document record) read as "en".
    persisted = persist_debate_result(
        _accepted_result("weave"), mongo_db, RuntimeConfig()
    )
    entry = OntologyEntryRepository(mongo_db).get(persisted.mpl_label)
    assert entry.language == "en"
