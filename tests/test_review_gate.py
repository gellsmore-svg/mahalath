"""Which stuck terms actually reach the operator (ADR-037)."""

from __future__ import annotations

import pytest

from mahalath.db.models import UndecidedItem
from mahalath.review_gate import (
    REVIEW_ESCALATION_THRESHOLD,
    ReviewError,
    accept_undecided,
    load_review_queue,
    needs_operator_review,
    reject_undecided,
    review_query,
)

THRESHOLD = 8.0


def _item(**kw) -> UndecidedItem:
    base = dict(
        decision_log_id=kw.pop("decision_log_id", "log-1"),
        term=kw.pop("term", "widget"),
        source_document_id="doc-A",
        reason="below_threshold",
        last_confidence=5.0,
        escalation_level=0,
    )
    base.update(kw)
    return UndecidedItem(**base)


# --- the rule -------------------------------------------------------------


@pytest.mark.parametrize(
    "item,expected,why",
    [
        (_item(escalation_level=0), False, "still being retried overnight"),
        (_item(escalation_level=1), False, "one retry left"),
        (_item(escalation_level=2), True, "attempts exhausted, still short"),
        (_item(escalation_level=5, last_confidence=9.0), False,
         "recursion got it above threshold — never bother a person"),
        (_item(reason="conflict", escalation_level=0), True,
         "one meaning or two is not settled by more recursion"),
        (_item(reason="moderator_block", escalation_level=0), True,
         "structural block, surface immediately"),
        (_item(reason="proposed_term", last_confidence=None, escalation_level=9),
         False, "never debated — nothing to review yet"),
        (_item(reason="proposed_term", last_confidence=4.0, escalation_level=2),
         True, "debated and still short"),
        (_item(last_confidence=None, escalation_level=3), True,
         "retried to the ceiling with no score is still stuck"),
    ],
)
def test_gate_rule(item, expected, why) -> None:
    assert needs_operator_review(item, confidence_threshold=THRESHOLD) is expected, why


def test_threshold_is_attempts_not_a_second_score() -> None:
    """The gate must not introduce a confidence number of its own."""
    just_under = _item(escalation_level=REVIEW_ESCALATION_THRESHOLD, last_confidence=7.9)
    just_over = _item(escalation_level=REVIEW_ESCALATION_THRESHOLD, last_confidence=8.0)
    assert needs_operator_review(just_under, confidence_threshold=THRESHOLD)
    assert not needs_operator_review(just_over, confidence_threshold=THRESHOLD)


def test_python_rule_and_mongo_query_agree(mongo_db) -> None:
    """Two expressions of one rule; a divergence between them is the bug."""
    items = [
        _item(decision_log_id=f"log-{i}", escalation_level=e, reason=r, last_confidence=c)
        for i, (e, r, c) in enumerate([
            (0, "below_threshold", 5.0), (1, "below_threshold", 5.0),
            (2, "below_threshold", 5.0), (3, "below_threshold", 9.5),
            (0, "conflict", 2.0), (4, "moderator_block", None),
            (9, "proposed_term", None), (2, "proposed_term", 4.0),
            (3, "iteration_cap", None), (2, "iteration_cap", 7.99),
        ])
    ]
    for item in items:
        mongo_db.undecided_queue.insert_one(item.model_dump())

    by_rule = {
        i.decision_log_id for i in items
        if needs_operator_review(i, confidence_threshold=THRESHOLD)
    }
    by_query = {
        d["decision_log_id"]
        for d in mongo_db.undecided_queue.find(
            review_query(confidence_threshold=THRESHOLD)
        )
    }
    assert by_rule == by_query, f"rule={by_rule} query={by_query}"


def test_queue_reports_what_is_still_being_retried(mongo_db) -> None:
    """An empty review list means 'nothing needs you', not 'nothing happening'."""
    for i, escalation in enumerate([0, 0, 1, 2]):
        mongo_db.undecided_queue.insert_one(
            _item(decision_log_id=f"log-{i}", escalation_level=escalation).model_dump()
        )
    queue = load_review_queue(mongo_db, confidence_threshold=THRESHOLD)
    assert len(queue.awaiting) == 1
    assert queue.still_retrying == 3
    assert queue.total_pending == 4


# --- operator actions -----------------------------------------------------


def test_accept_creates_an_entry_and_clears_the_queue(mongo_db) -> None:
    from mahalath.config import RuntimeConfig

    mongo_db.decision_log.insert_one({
        "decision_log_id": "log-A", "term": "widget", "source_document_id": "doc-A",
        "outcome": "undecided", "iterations_used": 3, "final_confidence": 6.0,
        "messages": [{"iteration": 3, "role": "synthesis_explorer",
                      "content": '{"definition": "A widget is a unit of work.", "confidence": 6.0}'}],
    })
    mongo_db.undecided_queue.insert_one(
        _item(decision_log_id="log-A", escalation_level=2).model_dump()
    )
    label = accept_undecided(mongo_db, "log-A", RuntimeConfig(), note="close enough")
    assert label.startswith("MPL-")

    entry = mongo_db.ontology_entries.find_one({"_id": label})
    assert entry["definitions"][0]["text"] == "A widget is a unit of work."
    assert mongo_db.undecided_queue.count_documents({"decision_log_id": "log-A"}) == 0

    audit = mongo_db.operator_decisions.find_one({"decision_log_id": "log-A"})
    assert audit["action"] == "accept" and audit["note"] == "close enough"
    assert audit["escalation_level"] == 2, "records how stuck it was"


def test_reject_removes_without_creating_an_entry(mongo_db) -> None:
    mongo_db.undecided_queue.insert_one(
        _item(decision_log_id="log-B", escalation_level=2).model_dump()
    )
    reject_undecided(mongo_db, "log-B", note="not a real term")
    assert mongo_db.undecided_queue.count_documents({}) == 0
    assert mongo_db.ontology_entries.count_documents({}) == 0
    assert mongo_db.operator_decisions.find_one({"action": "reject"})["note"] == (
        "not a real term"
    )


def test_accept_without_a_definition_refuses(mongo_db) -> None:
    from mahalath.config import RuntimeConfig

    mongo_db.undecided_queue.insert_one(
        _item(decision_log_id="log-C", escalation_level=2).model_dump()
    )
    with pytest.raises(ReviewError, match="no definition"):
        accept_undecided(mongo_db, "log-C", RuntimeConfig())


def test_acting_on_an_unknown_item_refuses(mongo_db) -> None:
    with pytest.raises(ReviewError, match="no undecided item"):
        reject_undecided(mongo_db, "nope")
