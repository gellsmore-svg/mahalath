"""REM re-review tests against a live MongoDB test db.

Covers: accept-on-re-debate promotes to ontology + removes from queue;
still-undecided re-debate increments escalation + writes new audit
without duplicating the queue row; max_escalation skip; missing-
context skip; max_items cap.
"""

from __future__ import annotations

import json

from mahalath.adapters import MockAdapter
from mahalath.config import AppConfig, MongoConfig
from mahalath.db.models import (
    DocumentRecord,
    UndecidedItem,
)
from mahalath.db.repositories import (
    ActionProposalRepository,  # noqa: F401 - confirms import surface
    AgentExchangeRepository,
    DecisionLogRepository,
    DocumentRepository,
    OntologyEntryRepository,
    UndecidedQueueRepository,
)
from mahalath.debate import PRECISION_CRITIC, SYNTHESIS_EXPLORER
from mahalath.debate import SPEAKER_TAG_PRECISION_CRITIC, SPEAKER_TAG_SYNTHESIS_EXPLORER
from mahalath.rem import rem_review


def _seed_document(mongo_db, document_id="doc-1") -> DocumentRecord:
    record = DocumentRecord(
        document_id=document_id,
        source_path="input/x.md",
        archive_path="processed/x.md",
        checksum_sha256="a" * 64,
        title="Sample",
        byte_size=1,
        char_count=1,
    )
    DocumentRepository(mongo_db).insert(record)
    return record


def _seed_undecided(
    mongo_db, *, decision_log_id, term, context, escalation_level=0,
    document_id="doc-1",
) -> UndecidedItem:
    item = UndecidedItem(
        decision_log_id=decision_log_id,
        term=term,
        source_document_id=document_id,
        reason="below_threshold",
        context=context,
        last_confidence=6.0,
        escalation_level=escalation_level,
    )
    UndecidedQueueRepository(mongo_db).insert(item)
    return item


def _json_response(definition: str, confidence: float, *, key: str = "critique") -> str:
    return json.dumps(
        {"definition": definition, key: "ok", "confidence": confidence}
    )


def test_rem_accepts_on_re_debate_and_removes_from_queue(mongo_db) -> None:
    config = AppConfig(mongo=MongoConfig(database="mahalath_pytest"))
    _seed_document(mongo_db)
    _seed_undecided(
        mongo_db,
        decision_log_id="dl-pending-1",
        term="agonist",
        context="An agonist binds to a receptor and activates it.",
    )

    adapter = MockAdapter(
        responses={
            SPEAKER_TAG_PRECISION_CRITIC: _json_response("Definition.", 9.0),
            SPEAKER_TAG_SYNTHESIS_EXPLORER: _json_response("Definition.", 8.5, key="rationale"),
        }
    )
    result = rem_review(config, mongo_db, adapter, max_items=10)
    assert result.items_reviewed == 1
    assert result.items_accepted == 1
    assert result.items_still_undecided == 0
    assert result.accepted_labels == ["MPL-001"]

    # Queue is now empty for this term
    assert UndecidedQueueRepository(mongo_db).list_pending() == []
    # Ontology entry exists
    assert OntologyEntryRepository(mongo_db).get("MPL-001") is not None


def test_rem_still_undecided_updates_queue_in_place(mongo_db) -> None:
    config = AppConfig(mongo=MongoConfig(database="mahalath_pytest"))
    _seed_document(mongo_db)
    _seed_undecided(
        mongo_db,
        decision_log_id="dl-pending-2",
        term="murky",
        context="murky context",
    )

    adapter = MockAdapter(
        responses={
            SPEAKER_TAG_PRECISION_CRITIC: _json_response("Definition.", 5.0),
            SPEAKER_TAG_SYNTHESIS_EXPLORER: _json_response("Definition.", 6.0, key="rationale"),
        }
    )
    result = rem_review(
        config, mongo_db, adapter,
        max_items=10, max_escalation=3,
    )
    assert result.items_reviewed == 1
    assert result.items_accepted == 0
    assert result.items_still_undecided == 1

    # One queue row remains (NOT two) with escalation incremented
    pending = UndecidedQueueRepository(mongo_db).list_pending()
    assert len(pending) == 1
    assert pending[0].decision_log_id == "dl-pending-2"
    assert pending[0].escalation_level == 1
    assert pending[0].last_confidence == 5.0  # min(5.0, 6.0)

    # New audit chain present (new decision_log_id from re-debate)
    decision_count = mongo_db.decision_log.count_documents({"term": "murky"})
    assert decision_count >= 1  # at least the re-debate run

    # Exchanges from the re-debate persisted
    exchange_count = mongo_db.agent_exchanges.count_documents({})
    assert exchange_count >= 2  # one iteration × two agents minimum


def test_rem_skips_items_past_max_escalation(mongo_db) -> None:
    config = AppConfig(mongo=MongoConfig(database="mahalath_pytest"))
    _seed_document(mongo_db)
    _seed_undecided(
        mongo_db, decision_log_id="dl-stale",
        term="stale", context="ctx", escalation_level=3,
    )
    adapter = MockAdapter(
        responses={
            SPEAKER_TAG_PRECISION_CRITIC: _json_response(".", 9.5),
            SPEAKER_TAG_SYNTHESIS_EXPLORER: _json_response(".", 9.5, key="rationale"),
        }
    )
    result = rem_review(config, mongo_db, adapter, max_escalation=3)
    assert result.items_reviewed == 0
    assert result.items_skipped_max_escalation == 1
    # Adapter was never called because the only eligible item was skipped
    assert adapter.calls == []
    # Queue row preserved
    assert len(UndecidedQueueRepository(mongo_db).list_pending()) == 1


def test_rem_skips_items_with_missing_context(mongo_db) -> None:
    config = AppConfig(mongo=MongoConfig(database="mahalath_pytest"))
    _seed_document(mongo_db)
    # Pre-S2.13 row that doesn't carry context
    UndecidedQueueRepository(mongo_db).insert(UndecidedItem(
        decision_log_id="dl-no-ctx",
        term="legacy",
        source_document_id="doc-1",
        reason="below_threshold",
        context=None,
        last_confidence=6.0,
        escalation_level=0,
    ))
    adapter = MockAdapter()
    result = rem_review(config, mongo_db, adapter)
    assert result.items_reviewed == 0
    assert result.items_skipped_max_escalation == 1
    assert adapter.calls == []


def test_rem_caps_at_max_items(mongo_db) -> None:
    config = AppConfig(mongo=MongoConfig(database="mahalath_pytest"))
    _seed_document(mongo_db)
    for i in range(5):
        _seed_undecided(
            mongo_db, decision_log_id=f"dl-{i}",
            term=f"term-{i}", context=f"ctx-{i}",
        )
    adapter = MockAdapter(
        responses={
            SPEAKER_TAG_PRECISION_CRITIC: _json_response(".", 9.5),
            SPEAKER_TAG_SYNTHESIS_EXPLORER: _json_response(".", 9.5, key="rationale"),
        }
    )
    result = rem_review(config, mongo_db, adapter, max_items=2)
    assert result.items_reviewed == 2
    assert result.items_accepted == 2
    # Three items remain pending
    assert len(UndecidedQueueRepository(mongo_db).list_pending()) == 3
