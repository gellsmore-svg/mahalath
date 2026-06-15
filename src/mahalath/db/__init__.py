"""Mahalath MongoDB layer.

Collections (see docs/architecture-decisions.md ADR-001, ADR-017):

- documents         ingested source records
- ontology_entries  flat dictionary of accepted entries, _id = MPL label
- ontology_tree     parent/child edges over MPL labels (hybrid flat+tree)
- decision_log      per-term debate audit (final outcome + score)
- agent_exchanges   per-iteration prompt/response records
- undecided_queue   items routed for human/escalation review

Logical separation from Tirzah is by database name (`mahalath_dev`
default per ADR-017); the same MongoDB instance hosts both.
"""

from mahalath.db.client import close_all, get_client, get_database
from mahalath.db.indexes import ensure_indexes
from mahalath.db.repositories import (
    ActionProposalRepository,
    AgentExchangeRepository,
    DecisionLogRepository,
    DefinitionContextRepository,
    DocumentRepository,
    OntologyEntryRepository,
    OntologyReviewRepository,
    OntologyTreeRepository,
    UndecidedQueueRepository,
)

__all__ = [
    "close_all",
    "get_client",
    "get_database",
    "ensure_indexes",
    "ActionProposalRepository",
    "AgentExchangeRepository",
    "DecisionLogRepository",
    "DefinitionContextRepository",
    "DocumentRepository",
    "OntologyEntryRepository",
    "OntologyReviewRepository",
    "OntologyTreeRepository",
    "UndecidedQueueRepository",
]
