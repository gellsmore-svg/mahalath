"""Pydantic models for the Mahalath MongoDB collections.

These are validation contracts only; persistence happens in
repositories.py. Schema versioning: every record carries
`schema_version: int = 1`; migrations bump this.

ID conventions:

- `documents._id`         ObjectId (Mongo-generated); `document_id` is a
                          stable UUID used as the cross-collection ref.
- `ontology_entries._id`  set to the MPL label string at write time so
                          references remain readable.
- everything else uses Mongo-generated ObjectId on `_id` and carries its
  own UUID-style stable id field (`decision_log_id`, etc.).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


SCHEMA_VERSION = 1


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _MahalathModel(BaseModel):
    """Base config: ignore Mongo's `_id` and other extras during validation."""

    model_config = ConfigDict(extra="ignore")


class DocumentRecord(_MahalathModel):
    """An ingested source document.

    `style_overlay_path` (optional) overrides
    `runtime.style_overlay_path` for this specific document, so a
    single Mahalath database can host multiple corpora in parallel
    with their own author-voice notes. None means "fall back to the
    runtime-level overlay (if any)".
    """

    document_id: str = Field(default_factory=lambda: str(uuid4()))
    source_path: str
    archive_path: str
    checksum_sha256: str
    title: str | None = None
    byte_size: int
    char_count: int
    ingested_at: datetime = Field(default_factory=_utcnow)
    processed_at: datetime | None = None
    style_overlay_path: str | None = None
    schema_version: int = SCHEMA_VERSION


class DefinitionVersion(_MahalathModel):
    """One agent-authored definition of an ontology entry."""

    text: str
    language: str = "en"
    model_used: str | None = None
    decision_log_id: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class OntologyEntry(_MahalathModel):
    """A flat-dictionary entry, keyed by MPL label.

    `mpl_label` is also written into `_id` at insert time by the
    repository. The hierarchical position lives in `ontology_tree`
    edges; storing `parent_label` here too is a denormalisation for
    fast reads.

    `references_labels` lists the other MPL labels mentioned in any
    of this entry's definitions (extracted on write). `is_stale` is
    set by `mark_dependents_stale` when an upstream entry changes;
    each change appends to `stale_reasons` so the audit chain shows
    why this entry's definition or hierarchy may no longer be valid.
    """

    mpl_label: str
    canonical_term: str
    aliases: list[str] = Field(default_factory=list)
    parent_label: str | None = None
    definitions: list[DefinitionVersion] = Field(default_factory=list)
    relations: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float
    status: str = "accepted"
    source_document_ids: list[str] = Field(default_factory=list)
    decision_log_id: str | None = None
    references_labels: list[str] = Field(default_factory=list)
    is_stale: bool = False
    stale_reasons: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    schema_version: int = SCHEMA_VERSION


class OntologyTreeEdge(_MahalathModel):
    """A parent/child edge between two MPL labels."""

    parent_label: str
    child_label: str
    relation_type: str = "child_of"
    created_at: datetime = Field(default_factory=_utcnow)
    schema_version: int = SCHEMA_VERSION


class DebateMessage(_MahalathModel):
    iteration: int
    role: str  # precision_critic | synthesis_explorer | moderator
    content: str
    confidence: float | None = None
    model: str | None = None


class DecisionLogEntry(_MahalathModel):
    """Per-term debate audit record.

    `outcome` is one of: accepted | rejected | undecided | split.
    `resulting_mpl_labels` lists the MPL labels created by this debate
    (zero for rejected/undecided; one for plain accept; two for split).
    """

    decision_log_id: str = Field(default_factory=lambda: str(uuid4()))
    term: str
    source_document_id: str
    messages: list[DebateMessage] = Field(default_factory=list)
    final_confidence: float | None = None
    iterations_used: int = 0
    outcome: str = "undecided"
    resulting_mpl_labels: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)
    schema_version: int = SCHEMA_VERSION


class AgentExchange(_MahalathModel):
    """One adapter call: prompt + response + confidence."""

    decision_log_id: str
    iteration: int
    role: str
    model: str
    prompt: str
    response: str
    confidence: float | None = None
    started_at: datetime = Field(default_factory=_utcnow)
    finished_at: datetime | None = None
    duration_ms: int | None = None
    schema_version: int = SCHEMA_VERSION


class UndecidedItem(_MahalathModel):
    """A term routed to the undecided queue.

    `reason` is one of: below_threshold | iteration_cap | conflict |
    moderator_block.

    `context` holds the source-document snippet that was the input to
    the original debate, so the REM re-review job can re-run debate
    with the same input without re-extracting from the document.
    """

    decision_log_id: str
    term: str
    source_document_id: str
    reason: str
    context: str | None = None
    last_confidence: float | None = None
    escalation_level: int = 0
    created_at: datetime = Field(default_factory=_utcnow)
    schema_version: int = SCHEMA_VERSION


class OntologyReview(_MahalathModel):
    """A record of one hierarchy-review pass (one adapter call).

    Generated by `mahalath.hierarchy.run_hierarchy_review`. Each
    `ActionProposal` it produces references it via
    `source_ontology_review_id`.

    `triggered_by` is one of: post_accept | rem_audit | manual.
    """

    review_id: str = Field(default_factory=lambda: str(uuid4()))
    triggered_by: str
    focus_mpl_label: str | None = None
    source_decision_log_id: str | None = None
    model: str
    prompt: str
    response: str
    duration_ms: int
    actions_count: int = 0
    no_actions_reason: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    schema_version: int = SCHEMA_VERSION


class ActionProposal(_MahalathModel):
    """An agent-proposed ontology action (parent assignment, alias, etc.).

    Every proposal is written to `action_proposals` regardless of
    outcome so the rationale and decision chain are queryable forever.

    `action_type` is one of: propose_parent | propose_alias |
    propose_merge | propose_split.
    `status` is one of: proposed | applied | pending_review | invalid |
    rejected.
    """

    proposal_id: str = Field(default_factory=lambda: str(uuid4()))
    action_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    confidence: float = 0.0
    proposed_by: str | None = None
    source_decision_log_id: str | None = None
    source_ontology_review_id: str | None = None
    status: str = "proposed"
    created_at: datetime = Field(default_factory=_utcnow)
    applied_at: datetime | None = None
    rejection_reason: str | None = None
    application_result: dict[str, Any] = Field(default_factory=dict)
    # Operator decision trail. operator_decision is one of:
    # accepted | rejected | rolled_back | None (no operator action taken).
    operator_decision: str | None = None
    operator_decision_at: datetime | None = None
    operator_note: str | None = None
    schema_version: int = SCHEMA_VERSION
