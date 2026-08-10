"""High-level ontology management: persist debate results into MongoDB.

This module is the seam between the pure-function debate loop and the
persistent ontology. Given a DebateResult it:

  - assigns an MPL label using the labels module (top-level by default,
    optionally under a chosen parent),
  - writes an OntologyEntry (flat dictionary) plus an OntologyTreeEdge
    when there is a parent,
  - writes the DecisionLogEntry recording the debate's outcome,
  - writes every AgentExchange so the per-iteration audit is queryable,
  - on undecided outcome, routes to the undecided queue with a reason
    derived from the run.

Contradistinct splits (DQ-005), inheritance of multiple parents,
relation edges other than child-of, and definition supersedence are
deferred to later stages; the schema accommodates them but Stage 1
exercises only the accepted/undecided base case.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pymongo.database import Database

from mahalath import labels
from mahalath.config import RuntimeConfig
from mahalath.db.models import (
    DecisionLogEntry,
    DefinitionVersion,
    OntologyEntry,
    OntologyTreeEdge,
    UndecidedItem,
)
from mahalath.db.repositories import (
    AgentExchangeRepository,
    DecisionLogRepository,
    OntologyEntryRepository,
    OntologyTreeRepository,
    UndecidedQueueRepository,
)
from mahalath.debate import DebateResult

ACCEPTED = "accepted"
UNDECIDED = "undecided"


@dataclass
class PersistResult:
    outcome: str
    decision_log_id: str
    ontology_entry: OntologyEntry | None = None
    undecided_item: UndecidedItem | None = None
    mpl_label: str | None = None


def persist_debate_result(
    result: DebateResult,
    db: Database,
    runtime: RuntimeConfig,
    *,
    parent_label: str | None = None,
    aliases: list[str] | None = None,
    extra_relations: list[dict[str, Any]] | None = None,
    adapter=None,
) -> PersistResult:
    """Persist a DebateResult into the ontology and audit collections.

    Caller may pin a `parent_label` (must be an existing accepted entry)
    or leave it None to assign at the top level. `aliases` and
    `extra_relations` flow through to the OntologyEntry record.

    When ``adapter`` is provided and ``runtime.generate_detailed_definitions``
    is true, a longer ``detailed_text`` is generated best-effort for the
    new definition after accept.
    """
    decision_repo = DecisionLogRepository(db)
    exchange_repo = AgentExchangeRepository(db)

    if result.outcome == ACCEPTED:
        ontology_entry, mpl_label = _write_accepted(
            result,
            db,
            parent_label=parent_label,
            aliases=aliases,
            extra_relations=extra_relations,
        )
        decision_repo.insert(
            _decision_log_entry(result, ACCEPTED, [mpl_label])
        )
        _persist_exchanges(result, exchange_repo)
        _maybe_enrich_detailed(
            result, db, runtime, mpl_label, definition_index=0, adapter=adapter,
        )
        # Reload so callers see detailed_text when generation succeeded.
        if ontology_entry is not None:
            refreshed = OntologyEntryRepository(db).get(mpl_label)
            if refreshed is not None:
                ontology_entry = refreshed
        return PersistResult(
            outcome=ACCEPTED,
            decision_log_id=result.decision_log_id,
            ontology_entry=ontology_entry,
            mpl_label=mpl_label,
        )

    if result.outcome == UNDECIDED:
        decision_repo.insert(_decision_log_entry(result, UNDECIDED, []))
        _persist_exchanges(result, exchange_repo)
        item = _route_to_undecided(result, db, runtime)
        return PersistResult(
            outcome=UNDECIDED,
            decision_log_id=result.decision_log_id,
            undecided_item=item,
        )

    raise ValueError(
        f"persist_debate_result: unsupported outcome {result.outcome!r}; "
        "Stage 1 handles accepted and undecided only."
    )


def _write_accepted(
    result: DebateResult,
    db: Database,
    *,
    parent_label: str | None,
    aliases: list[str] | None,
    extra_relations: list[dict[str, Any]] | None,
) -> tuple[OntologyEntry, str]:
    if result.final_definition is None or result.final_confidence is None:
        raise ValueError(
            "Accepted debate result missing final_definition or final_confidence."
        )

    entries = OntologyEntryRepository(db)
    tree = OntologyTreeRepository(db)
    mpl_label = _assign_label(entries, parent_label=parent_label)

    # Lexicon membership (ADR-028): the entry belongs to the language
    # of the document that evidenced it. Missing/legacy docs read "en".
    src_doc = db.documents.find_one(
        {"document_id": result.source_document_id}, {"language": 1}
    )
    entry_language = (src_doc or {}).get("language") or "en"

    context_id = _resolve_context_id(db, result.final_context_name)
    # Materialise the ancestor chain at insert: parent's own path plus
    # the parent itself (empty for a top-level entry). See paths.py.
    if parent_label is not None:
        from mahalath.paths import resolved_path
        parent_entry = entries.get(parent_label)
        entry_path = (
            [*resolved_path(db, parent_entry), parent_label]
            if parent_entry is not None
            else [parent_label]
        )
    else:
        entry_path = []
    entry = OntologyEntry(
        mpl_label=mpl_label,
        canonical_term=result.term,
        language=entry_language,
        parent_label=parent_label,
        path=entry_path,
        confidence=result.final_confidence,
        definitions=[
            DefinitionVersion(
                text=result.final_definition,
                language=entry_language,
                model_used=_model_used_in(result),
                decision_log_id=result.decision_log_id,
                context_id=context_id,
                # Multi-agent agreement for THIS definition (min(pc, se)
                # at accept time); the entry-level confidence can drift
                # later, this snapshot doesn't.
                consensus_score=result.final_confidence,
            )
        ],
        source_document_ids=[result.source_document_id],
        decision_log_id=result.decision_log_id,
        aliases=aliases or [],
        relations=extra_relations or [],
    )
    entries.insert(entry)

    if parent_label is not None:
        tree.add_edge(
            OntologyTreeEdge(parent_label=parent_label, child_label=mpl_label)
        )

    # Populate references_labels from the entry's definitions, so the
    # reverse-index is built incrementally as entries land — and
    # retro-link older entries whose definitions mention this new term,
    # so the reverse index doesn't go stale as the ontology grows.
    from mahalath.staleness import retro_link_new_entry, update_references
    update_references(db, mpl_label)
    retro_link_new_entry(db, mpl_label)

    return entry, mpl_label


def _maybe_enrich_detailed(
    result: DebateResult,
    db: Database,
    runtime: RuntimeConfig,
    mpl_label: str,
    *,
    definition_index: int = -1,
    adapter=None,
) -> None:
    """Best-effort detailed_text on the just-written definition."""
    if not getattr(runtime, "generate_detailed_definitions", True):
        return
    if adapter is None:
        return
    try:
        from mahalath.detailed import enrich_definition_with_detail
        from mahalath.style import load_style_overlay
        from mahalath.config import AppConfig

        # load_style_overlay expects AppConfig; build a minimal shell.
        config = AppConfig(runtime=runtime)
        style = load_style_overlay(config)
        enrich_definition_with_detail(
            db,
            mpl_label,
            adapter=adapter,
            definition_index=definition_index,
            style_overlay=style,
            source_snippet=result.context or None,
        )
    except Exception:  # noqa: BLE001 — never break accept
        pass


def _assign_label(
    entries: OntologyEntryRepository, *, parent_label: str | None
) -> str:
    if parent_label is None:
        return labels.next_top_level(entries.all_labels())
    existing_children = entries.labels_under_parent(parent_label)
    return labels.next_child(parent_label, existing_children)


def _decision_log_entry(
    result: DebateResult, outcome: str, resulting_labels: list[str]
) -> DecisionLogEntry:
    return DecisionLogEntry(
        decision_log_id=result.decision_log_id,
        term=result.term,
        source_document_id=result.source_document_id,
        messages=result.messages,
        final_confidence=result.final_confidence,
        iterations_used=result.iterations_used,
        outcome=outcome,
        resulting_mpl_labels=resulting_labels,
    )


def _persist_exchanges(
    result: DebateResult, exchange_repo: AgentExchangeRepository
) -> None:
    for exchange in result.exchanges:
        exchange_repo.insert(exchange)


def _route_to_undecided(
    result: DebateResult, db: Database, runtime: RuntimeConfig
) -> UndecidedItem:
    reason = (
        "iteration_cap"
        if result.iterations_used >= runtime.max_iterations_per_term
        else "below_threshold"
    )
    item = UndecidedItem(
        decision_log_id=result.decision_log_id,
        term=result.term,
        source_document_id=result.source_document_id,
        reason=reason,
        context=result.context or None,
        last_confidence=result.final_confidence,
    )
    UndecidedQueueRepository(db).insert(item)
    return item


def persist_decision_audit(
    result: DebateResult,
    db: Database,
    *,
    outcome: str,
    resulting_labels: list[str] | None = None,
) -> None:
    """Write decision_log + agent_exchanges only.

    Used by REM re-review when re-debate produces another undecided
    outcome: we want the new attempt's audit trail to be queryable
    (per-iteration prompts/responses, final confidence, message
    history) but we do NOT want to create a duplicate
    UndecidedItem — the existing queue row is updated in place
    instead. Also useful for any future job that wants to record a
    debate run without changing ontology state.
    """
    DecisionLogRepository(db).insert(
        _decision_log_entry(result, outcome, resulting_labels or [])
    )
    _persist_exchanges(result, AgentExchangeRepository(db))


@dataclass
class RedebateResult:
    """Outcome of re-running the debate on an already-accepted entry."""

    mpl_label: str
    term: str
    outcome: str                       # accepted | undecided
    old_definition: str
    old_model_used: str | None
    new_definition: str | None
    new_confidence: float | None
    new_model_used: str | None
    applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mpl_label": self.mpl_label,
            "term": self.term,
            "outcome": self.outcome,
            "old_definition": self.old_definition,
            "old_model_used": self.old_model_used,
            "new_definition": self.new_definition,
            "new_confidence": self.new_confidence,
            "new_model_used": self.new_model_used,
            "applied": self.applied,
        }


def redebate_entry(
    db: Database,
    mpl_label: str,
    context: str,
    adapter,
    runtime: RuntimeConfig,
    *,
    style_overlay: str | None = None,
    apply: bool = False,
) -> RedebateResult:
    """Re-run the debate loop on an existing entry to refresh its definition.

    Unlike `persist_debate_result` (which mints a NEW label), this
    refreshes an entry already in the ontology: it runs the standard
    PrecisionCritic/SynthesisExplorer loop (cross-family under the live
    config) over `context` (the entry's source-document section), and on
    `apply=True` with an accepted outcome APPENDS the new definition to
    the entry — the prior definition is preserved in history (source
    preservation), the new one becomes the active (latest) version.

    The new definition is pinned to the entry's existing frame so a
    quality refresh never accidentally re-frames the term. On apply,
    the debate's decision_log + agent_exchanges are persisted for
    audit; a dry run writes nothing at all. Built for the M-B
    single-model→cross-family cleanup (S2.50); reusable for any
    quality re-debate.
    """
    from mahalath.db.repositories import DefinitionContextRepository
    from mahalath.debate import run_debate
    from mahalath.staleness import mark_dependents_stale, update_references

    entries = OntologyEntryRepository(db)
    entry = entries.get(mpl_label)
    if entry is None:
        raise ValueError(f"redebate_entry: no entry {mpl_label!r}")
    if not entry.definitions:
        raise ValueError(f"redebate_entry: {mpl_label!r} has no definition")

    current = entry.definitions[-1]
    src_doc = db.documents.find_one(
        {"document_id": {"$in": entry.source_document_ids}},
        {"document_id": 1},
    )
    source_document_id = (
        (src_doc or {}).get("document_id")
        or (entry.source_document_ids[0] if entry.source_document_ids else "")
    )

    available_contexts = [
        {"name": c.name, "description": c.description}
        for c in DefinitionContextRepository(db).all(kind="frame")
    ]

    result = run_debate(
        entry.canonical_term,
        context,
        source_document_id,
        adapter,
        runtime,
        style_overlay=style_overlay,
        available_contexts=available_contexts or None,
    )

    out = RedebateResult(
        mpl_label=mpl_label,
        term=entry.canonical_term,
        outcome=result.outcome,
        old_definition=current.text,
        old_model_used=current.model_used,
        new_definition=result.final_definition,
        new_confidence=result.final_confidence,
        new_model_used=_model_used_in(result),
    )

    if not apply or result.outcome != ACCEPTED:
        return out

    # Persist the debate audit (decision_log + exchanges), then append
    # the refreshed definition. Pin it to the entry's current frame so
    # the refresh stays in-frame; entry-level confidence advances to the
    # new cross-family agreement.
    persist_decision_audit(result, db, outcome=ACCEPTED, resulting_labels=[mpl_label])
    now = datetime.now(timezone.utc)
    db.ontology_entries.update_one(
        {"_id": mpl_label},
        {
            "$push": {
                "definitions": {
                    "text": result.final_definition,
                    "language": entry.language,
                    "model_used": _model_used_in(result),
                    "decision_log_id": result.decision_log_id,
                    "context_id": current.context_id,
                    "consensus_score": result.final_confidence,
                    "created_at": now,
                }
            },
            "$set": {"confidence": result.final_confidence, "updated_at": now},
        },
    )
    update_references(db, mpl_label)
    if getattr(runtime, "generate_detailed_definitions", True):
        try:
            from mahalath.detailed import enrich_definition_with_detail
            from mahalath.style import load_style_overlay
            from mahalath.config import AppConfig

            enrich_definition_with_detail(
                db,
                mpl_label,
                adapter=adapter,
                definition_index=-1,
                style_overlay=load_style_overlay(AppConfig(runtime=runtime)),
                source_snippet=context or None,
            )
        except Exception:  # noqa: BLE001
            pass
    # The entry's definitional content just changed: propagate staleness
    # to dependents AND to any cross-language mapping on this endpoint
    # (ADR-029 — redefining an endpoint flags its mappings for re-audit).
    mark_dependents_stale(
        db, mpl_label,
        change_type="definition_redefined",
        note="cross-family re-debate refreshed the definition",
    )
    out.applied = True
    return out


def _model_used_in(result: DebateResult) -> str | None:
    """Pick a representative model name from the recorded exchanges."""
    for exchange in reversed(result.exchanges):
        if exchange.model:
            return exchange.model
    return None


def _resolve_context_id(db: Database, context_name: str | None) -> str | None:
    """Map a context name (e.g., 'structural') to its DefinitionContext id.

    Frames only: a debate's context_name must never resolve to an
    intent-taxonomy row (ADR-024 — intent is not a meaning frame).
    """
    if not context_name:
        return None
    from mahalath.db.repositories import DefinitionContextRepository
    ctx = DefinitionContextRepository(db).get_by_name(context_name, kind="frame")
    return ctx.context_id if ctx is not None else None


def backfill_language(db: Database) -> dict[str, int]:
    """One-shot M-A migration: stamp language='en' on entries and
    documents that pre-date ADR-028. Idempotent."""
    entries = db.ontology_entries.update_many(
        {"language": {"$exists": False}}, {"$set": {"language": "en"}}
    )
    documents = db.documents.update_many(
        {"language": {"$exists": False}}, {"$set": {"language": "en"}}
    )
    return {
        "entries_stamped": entries.modified_count,
        "documents_stamped": documents.modified_count,
    }
