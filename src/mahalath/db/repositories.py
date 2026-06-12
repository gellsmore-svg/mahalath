"""Thin repositories over the Mahalath collections.

Each repository wraps a single collection. Reads return Pydantic models;
writes accept models. Repositories are stateless; pass them an open
`Database` from `db.client.get_database`.

Deliberately not an ORM: MongoDB queries stay explicit at the call site.
Repositories just standardise the model<->BSON conversion so the rest of
the codebase doesn't deal with raw dicts.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pymongo.collection import Collection
from pymongo.database import Database

from mahalath.db.models import (
    ActionProposal,
    AgentExchange,
    DecisionLogEntry,
    DefinitionContext,
    DocumentRecord,
    OntologyEntry,
    OntologyReview,
    OntologyTreeEdge,
    UndecidedItem,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DocumentRepository:
    def __init__(self, db: Database) -> None:
        self._col: Collection = db.documents

    def insert(self, record: DocumentRecord) -> DocumentRecord:
        self._col.insert_one(record.model_dump())
        return record

    def find_by_checksum(self, checksum: str) -> DocumentRecord | None:
        doc = self._col.find_one({"checksum_sha256": checksum})
        return DocumentRecord.model_validate(doc) if doc else None

    def find_by_document_id(self, document_id: str) -> DocumentRecord | None:
        doc = self._col.find_one({"document_id": document_id})
        return DocumentRecord.model_validate(doc) if doc else None

    def mark_processed(self, document_id: str) -> None:
        self._col.update_one(
            {"document_id": document_id},
            {"$set": {"processed_at": _utcnow()}},
        )

    def count(self) -> int:
        return self._col.count_documents({})


class OntologyEntryRepository:
    def __init__(self, db: Database) -> None:
        self._col: Collection = db.ontology_entries

    def insert(self, entry: OntologyEntry) -> OntologyEntry:
        payload = entry.model_dump()
        payload["_id"] = entry.mpl_label
        # Stemming hint for the text index (decoupled override field,
        # see db/indexes.py). Only set for languages Mongo can stem;
        # everything else falls back to the index default silently.
        from mahalath.db.models import MONGO_STEM_LANGUAGES
        stem = MONGO_STEM_LANGUAGES.get(entry.language)
        if stem is not None and stem != "english":
            payload["text_language"] = stem
        self._col.insert_one(payload)
        return entry

    def get(self, mpl_label: str) -> OntologyEntry | None:
        doc = self._col.find_one({"_id": mpl_label})
        return OntologyEntry.model_validate(doc) if doc else None

    def all_labels(self) -> list[str]:
        return [doc["_id"] for doc in self._col.find({}, {"_id": 1})]

    def labels_under_parent(self, parent_label: str | None) -> list[str]:
        return [
            doc["_id"]
            for doc in self._col.find(
                {"parent_label": parent_label}, {"_id": 1}
            )
        ]

    def find_by_canonical_term(self, term: str) -> list[OntologyEntry]:
        return [
            OntologyEntry.model_validate(doc)
            for doc in self._col.find({"canonical_term": term})
        ]


class DefinitionContextRepository:
    def __init__(self, db: Database) -> None:
        self._col: Collection = db.definition_contexts

    def insert(self, ctx: DefinitionContext) -> DefinitionContext:
        self._col.insert_one(ctx.model_dump())
        return ctx

    def get(self, context_id: str) -> DefinitionContext | None:
        doc = self._col.find_one({"context_id": context_id})
        return DefinitionContext.model_validate(doc) if doc else None

    def get_by_name(
        self, name: str, *, kind: str | None = None
    ) -> DefinitionContext | None:
        """Look up by name; `kind` restricts to "frame" or "intent".

        Filtering happens after validation so legacy documents without
        the `kind` field read as frames (the model default).
        """
        doc = self._col.find_one({"name": name})
        if not doc:
            return None
        ctx = DefinitionContext.model_validate(doc)
        if kind is not None and ctx.kind != kind:
            return None
        return ctx

    def all(self, *, kind: str | None = None) -> list[DefinitionContext]:
        """All contexts, oldest first; `kind` restricts to one taxonomy.

        Filtering happens after validation so legacy documents without
        the `kind` field read as frames (the model default).
        """
        items = [
            DefinitionContext.model_validate(doc)
            for doc in self._col.find().sort("created_at", 1)
        ]
        if kind is None:
            return items
        return [c for c in items if c.kind == kind]


class OntologyTreeRepository:
    def __init__(self, db: Database) -> None:
        self._col: Collection = db.ontology_tree

    def add_edge(self, edge: OntologyTreeEdge) -> OntologyTreeEdge:
        self._col.insert_one(edge.model_dump())
        return edge

    def children_of(self, parent_label: str) -> list[str]:
        return [
            doc["child_label"]
            for doc in self._col.find(
                {"parent_label": parent_label}, {"child_label": 1}
            )
        ]


class DecisionLogRepository:
    def __init__(self, db: Database) -> None:
        self._col: Collection = db.decision_log

    def insert(self, entry: DecisionLogEntry) -> DecisionLogEntry:
        self._col.insert_one(entry.model_dump())
        return entry

    def get(self, decision_log_id: str) -> DecisionLogEntry | None:
        doc = self._col.find_one({"decision_log_id": decision_log_id})
        return DecisionLogEntry.model_validate(doc) if doc else None


class AgentExchangeRepository:
    def __init__(self, db: Database) -> None:
        self._col: Collection = db.agent_exchanges

    def insert(self, exchange: AgentExchange) -> AgentExchange:
        self._col.insert_one(exchange.model_dump())
        return exchange

    def for_decision(self, decision_log_id: str) -> list[AgentExchange]:
        cursor = self._col.find({"decision_log_id": decision_log_id}).sort(
            "iteration", 1
        )
        return [AgentExchange.model_validate(doc) for doc in cursor]


class UndecidedQueueRepository:
    def __init__(self, db: Database) -> None:
        self._col: Collection = db.undecided_queue

    def insert(self, item: UndecidedItem) -> UndecidedItem:
        self._col.insert_one(item.model_dump())
        return item

    def list_pending(self, *, limit: int = 100) -> list[UndecidedItem]:
        cursor = (
            self._col.find()
            .sort([("escalation_level", 1), ("created_at", 1)])
            .limit(limit)
        )
        return [UndecidedItem.model_validate(doc) for doc in cursor]


class OntologyReviewRepository:
    def __init__(self, db: Database) -> None:
        self._col: Collection = db.ontology_reviews

    def insert(self, review: OntologyReview) -> OntologyReview:
        self._col.insert_one(review.model_dump())
        return review

    def get(self, review_id: str) -> OntologyReview | None:
        doc = self._col.find_one({"review_id": review_id})
        return OntologyReview.model_validate(doc) if doc else None

    def for_decision(self, decision_log_id: str) -> list[OntologyReview]:
        return [
            OntologyReview.model_validate(doc)
            for doc in self._col.find(
                {"source_decision_log_id": decision_log_id}
            ).sort("created_at", 1)
        ]


class ActionProposalRepository:
    def __init__(self, db: Database) -> None:
        self._col: Collection = db.action_proposals

    def insert(self, proposal: ActionProposal) -> ActionProposal:
        self._col.insert_one(proposal.model_dump())
        return proposal

    def get(self, proposal_id: str) -> ActionProposal | None:
        doc = self._col.find_one({"proposal_id": proposal_id})
        return ActionProposal.model_validate(doc) if doc else None

    def by_status(self, status: str) -> list[ActionProposal]:
        return [
            ActionProposal.model_validate(doc)
            for doc in self._col.find({"status": status}).sort("created_at", 1)
        ]

    def for_decision(self, decision_log_id: str) -> list[ActionProposal]:
        return [
            ActionProposal.model_validate(doc)
            for doc in self._col.find(
                {"source_decision_log_id": decision_log_id}
            ).sort("created_at", 1)
        ]

    def for_review(self, review_id: str) -> list[ActionProposal]:
        return [
            ActionProposal.model_validate(doc)
            for doc in self._col.find(
                {"source_ontology_review_id": review_id}
            ).sort("created_at", 1)
        ]
