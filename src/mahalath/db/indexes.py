"""Index descriptors and a one-shot `ensure_indexes` call.

Called from CLI startup so a fresh database picks up the right indexes
before any reads/writes happen. Idempotent: pymongo's `create_index`
returns existing index names instead of duplicating.
"""

from __future__ import annotations

from pymongo import ASCENDING, DESCENDING, TEXT
from pymongo.database import Database


def ensure_indexes(db: Database) -> dict[str, list[str]]:
    created: dict[str, list[str]] = {}

    created["documents"] = [
        db.documents.create_index("document_id", unique=True),
        db.documents.create_index("checksum_sha256", unique=True),
        db.documents.create_index("source_path"),
        db.documents.create_index([("ingested_at", DESCENDING)]),
    ]

    created["ontology_entries"] = [
        # `_id` doubles as the MPL label so no extra uniqueness index needed.
        db.ontology_entries.create_index("parent_label"),
        db.ontology_entries.create_index("canonical_term"),
        db.ontology_entries.create_index("status"),
        db.ontology_entries.create_index("references_labels"),  # for reverse lookups
        db.ontology_entries.create_index("is_stale"),
        db.ontology_entries.create_index([("updated_at", DESCENDING)]),
        # Fuzzy term search for the retrieval layer (S-A). Weighted so a
        # canonical-term hit outranks an alias hit, which outranks a hit
        # buried in a definition body. One text index per collection.
        db.ontology_entries.create_index(
            [("canonical_term", TEXT), ("aliases", TEXT), ("definitions.text", TEXT)],
            weights={"canonical_term": 10, "aliases": 5, "definitions.text": 1},
            name="ontology_text",
        ),
    ]

    created["ontology_tree"] = [
        db.ontology_tree.create_index(
            [("parent_label", ASCENDING), ("child_label", ASCENDING)],
            unique=True,
        ),
        db.ontology_tree.create_index("parent_label"),
        db.ontology_tree.create_index("child_label"),
    ]

    created["decision_log"] = [
        db.decision_log.create_index("decision_log_id", unique=True),
        db.decision_log.create_index("term"),
        db.decision_log.create_index("source_document_id"),
        db.decision_log.create_index([("created_at", DESCENDING)]),
    ]

    created["agent_exchanges"] = [
        db.agent_exchanges.create_index("decision_log_id"),
        db.agent_exchanges.create_index(
            [("decision_log_id", ASCENDING), ("iteration", ASCENDING)]
        ),
        db.agent_exchanges.create_index([("started_at", DESCENDING)]),
    ]

    created["undecided_queue"] = [
        db.undecided_queue.create_index("decision_log_id", unique=True),
        db.undecided_queue.create_index("term"),
        db.undecided_queue.create_index(
            [("escalation_level", ASCENDING), ("created_at", ASCENDING)]
        ),
    ]

    created["action_proposals"] = [
        db.action_proposals.create_index("proposal_id", unique=True),
        db.action_proposals.create_index("action_type"),
        db.action_proposals.create_index("status"),
        db.action_proposals.create_index("source_decision_log_id"),
        db.action_proposals.create_index("source_ontology_review_id"),
        db.action_proposals.create_index([("created_at", DESCENDING)]),
    ]

    created["ontology_reviews"] = [
        db.ontology_reviews.create_index("review_id", unique=True),
        db.ontology_reviews.create_index("triggered_by"),
        db.ontology_reviews.create_index("focus_mpl_label"),
        db.ontology_reviews.create_index("source_decision_log_id"),
        db.ontology_reviews.create_index([("created_at", DESCENDING)]),
    ]

    created["definition_contexts"] = [
        db.definition_contexts.create_index("context_id", unique=True),
        db.definition_contexts.create_index("name", unique=True),
        db.definition_contexts.create_index([("created_at", DESCENDING)]),
    ]

    return created
