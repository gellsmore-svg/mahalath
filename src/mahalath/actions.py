"""Agent-callable ontology actions and the dispatcher that applies them.

Where the Stage 1 debate loop answered "what does this term mean?",
actions answer "how should this term sit in the existing structure?":
is something its parent? is this an alias? is this a duplicate?

Action types in this slice:

  propose_parent  declare an existing top-level label should become the
                  parent of another existing top-level label. Auto-apply
                  at high confidence.
  propose_alias   add an alias string to an existing label. Auto-apply
                  at high confidence.
  propose_merge   declare two labels are duplicates; mark one superseded.
                  Destructive — always routes to review queue.
  propose_split   declare a label needs contradistinct variants.
                  Destructive — always routes to review queue.

Audit trail: every proposed action is written to `action_proposals`
regardless of outcome (applied / pending_review / invalid / rejected)
so the rationale and decision chain are queryable forever.

Design rule (ADR-018): MPL labels are IMMUTABLE once assigned.
Re-parenting only changes tree edges + the denormalised parent_label
field; the label string itself never changes. This preserves the
audit-log invariant that any historical reference to an MPL label
resolves to the same entity.

Re-parenting (changing the parent of a label that already has one) is
deliberately blocked in this slice. The agent can only attach parents
to currently top-level entries. Re-parenting can come later as a
separate action type with explicit operator confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Union

from pymongo.database import Database

from mahalath import labels
from mahalath.db.models import ActionProposal, OntologyTreeEdge
from mahalath.db.repositories import (
    ActionProposalRepository,
    OntologyEntryRepository,
    OntologyTreeRepository,
)


DESTRUCTIVE_TYPES = frozenset({"propose_merge", "propose_split"})
DEFAULT_AUTO_APPLY_THRESHOLD = 8.0
# Re-parenting (changing a child's existing parent to a different one) is
# potentially more disruptive than initial parent assignment, so it requires
# higher confidence to auto-apply. Operator can lower at dispatch time.
DEFAULT_REPARENT_THRESHOLD = 8.5


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- Action types ----------------------------------------------------------


@dataclass(frozen=True)
class _ActionBase:
    reason: str
    confidence: float
    proposed_by: str | None = None
    source_decision_log_id: str | None = None
    source_ontology_review_id: str | None = None


@dataclass(frozen=True)
class ProposeParent(_ActionBase):
    child_label: str = ""
    parent_label: str = ""

    @property
    def action_type(self) -> str:
        return "propose_parent"

    def payload(self) -> dict[str, Any]:
        return {
            "child_label": self.child_label,
            "parent_label": self.parent_label,
        }


@dataclass(frozen=True)
class ProposeAlias(_ActionBase):
    label: str = ""
    alias: str = ""

    @property
    def action_type(self) -> str:
        return "propose_alias"

    def payload(self) -> dict[str, Any]:
        return {"label": self.label, "alias": self.alias}


@dataclass(frozen=True)
class ProposeMerge(_ActionBase):
    keep_label: str = ""
    drop_label: str = ""

    @property
    def action_type(self) -> str:
        return "propose_merge"

    def payload(self) -> dict[str, Any]:
        return {
            "keep_label": self.keep_label,
            "drop_label": self.drop_label,
        }


@dataclass(frozen=True)
class ProposeSplit(_ActionBase):
    label: str = ""
    into: tuple[str, ...] = ()

    @property
    def action_type(self) -> str:
        return "propose_split"

    def payload(self) -> dict[str, Any]:
        return {"label": self.label, "into": list(self.into)}


Action = Union[ProposeParent, ProposeAlias, ProposeMerge, ProposeSplit]


# --- Result types ----------------------------------------------------------


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    reason: str | None = None


@dataclass(frozen=True)
class DispatchResult:
    proposal_id: str
    action_type: str
    status: str  # applied | pending_review | invalid
    detail: str
    payload: dict[str, Any]


# --- Parsing ----------------------------------------------------------------


def parse_actions(
    response_json: dict[str, Any],
    *,
    proposed_by: str | None = None,
    source_decision_log_id: str | None = None,
    source_ontology_review_id: str | None = None,
) -> list[Action]:
    """Parse the `actions` array from an agent JSON response.

    Silently skips entries with unknown types or missing required
    fields rather than raising; the dispatcher's `invalid` status
    handles per-action problems with full audit detail. Skipping at
    parse time is only for entries that can't be turned into any
    Action at all.
    """
    raw_actions = response_json.get("actions")
    if not isinstance(raw_actions, list):
        return []

    common = {
        "proposed_by": proposed_by,
        "source_decision_log_id": source_decision_log_id,
        "source_ontology_review_id": source_ontology_review_id,
    }

    result: list[Action] = []
    for raw in raw_actions:
        if not isinstance(raw, dict):
            continue
        action_type = raw.get("type")
        reason = str(raw.get("reason", "")).strip()
        confidence = _coerce_confidence(raw.get("confidence"))

        try:
            if action_type == "propose_parent":
                action: Action = ProposeParent(
                    child_label=str(raw.get("child_label", "")).strip(),
                    parent_label=str(raw.get("parent_label", "")).strip(),
                    reason=reason,
                    confidence=confidence,
                    **common,
                )
            elif action_type == "propose_alias":
                action = ProposeAlias(
                    label=str(raw.get("label", "")).strip(),
                    alias=str(raw.get("alias", "")).strip(),
                    reason=reason,
                    confidence=confidence,
                    **common,
                )
            elif action_type == "propose_merge":
                action = ProposeMerge(
                    keep_label=str(raw.get("keep_label", "")).strip(),
                    drop_label=str(raw.get("drop_label", "")).strip(),
                    reason=reason,
                    confidence=confidence,
                    **common,
                )
            elif action_type == "propose_split":
                into_raw = raw.get("into", [])
                if not isinstance(into_raw, list):
                    continue
                action = ProposeSplit(
                    label=str(raw.get("label", "")).strip(),
                    into=tuple(
                        str(x).strip() for x in into_raw if str(x).strip()
                    ),
                    reason=reason,
                    confidence=confidence,
                    **common,
                )
            else:
                continue
        except (TypeError, ValueError):
            continue
        result.append(action)

    return result


def _coerce_confidence(value: Any) -> float:
    try:
        c = float(value)
    except (TypeError, ValueError):
        return 0.0
    if c != c:  # NaN check
        return 0.0
    return max(0.0, min(10.0, c))


# --- Validation -------------------------------------------------------------


def validate(action: Action, db: Database) -> ValidationResult:
    if isinstance(action, ProposeParent):
        return _validate_parent(action, db)
    if isinstance(action, ProposeAlias):
        return _validate_alias(action, db)
    if isinstance(action, ProposeMerge):
        return _validate_merge(action, db)
    if isinstance(action, ProposeSplit):
        return _validate_split(action, db)
    return ValidationResult(False, f"unknown action type: {type(action).__name__}")


def _validate_parent(action: ProposeParent, db: Database) -> ValidationResult:
    if not action.child_label or not action.parent_label:
        return ValidationResult(False, "child_label or parent_label is empty")
    if action.child_label == action.parent_label:
        return ValidationResult(False, "child and parent are the same label")

    entries = OntologyEntryRepository(db)
    child = entries.get(action.child_label)
    if child is None:
        return ValidationResult(
            False, f"child label {action.child_label!r} does not exist"
        )
    parent = entries.get(action.parent_label)
    if parent is None:
        return ValidationResult(
            False, f"parent label {action.parent_label!r} does not exist"
        )

    # Re-parenting is allowed (S2.4); only reject a no-op (same parent).
    if child.parent_label == action.parent_label:
        return ValidationResult(
            False,
            f"child {action.child_label!r} already has parent "
            f"{action.parent_label!r}; no change needed",
        )

    try:
        parent_parsed = labels.parse(action.parent_label)
    except ValueError as exc:
        return ValidationResult(False, f"invalid parent label format: {exc}")
    if parent_parsed.suffix is not None:
        return ValidationResult(False, "cannot use a variant label as parent")

    if _has_cycle(db, new_parent=action.parent_label, new_child=action.child_label):
        return ValidationResult(False, "edge would introduce a cycle")

    return ValidationResult(True, None)


def is_reparenting(action: ProposeParent, db: Database) -> bool:
    """Return True if applying `action` would change a child's existing parent."""
    entries = OntologyEntryRepository(db)
    child = entries.get(action.child_label)
    return (
        child is not None
        and child.parent_label is not None
        and child.parent_label != action.parent_label
    )


def _has_cycle(db: Database, *, new_parent: str, new_child: str) -> bool:
    """Return True if adding parent->child would create a cycle.

    Walks forward (parent → child direction) from new_child through
    existing tree edges. If new_parent is reachable, the new edge
    would close a loop.
    """
    tree = OntologyTreeRepository(db)
    visited: set[str] = set()
    stack = [new_child]
    while stack:
        node = stack.pop()
        if node == new_parent:
            return True
        if node in visited:
            continue
        visited.add(node)
        stack.extend(tree.children_of(node))
    return False


def _validate_alias(action: ProposeAlias, db: Database) -> ValidationResult:
    alias_clean = action.alias.strip()
    if not alias_clean:
        return ValidationResult(False, "alias is empty")
    if not action.label:
        return ValidationResult(False, "label is empty")

    entries = OntologyEntryRepository(db)
    entry = entries.get(action.label)
    if entry is None:
        return ValidationResult(
            False, f"label {action.label!r} does not exist"
        )
    if alias_clean.casefold() == entry.canonical_term.casefold():
        return ValidationResult(False, "alias equals canonical term")
    if any(alias_clean.casefold() == a.casefold() for a in entry.aliases):
        return ValidationResult(False, "alias already present on this entry")
    collisions = entries.find_by_canonical_term(alias_clean)
    if collisions:
        return ValidationResult(
            False,
            f"alias is already the canonical term of {collisions[0].mpl_label!r}",
        )
    return ValidationResult(True, None)


def _validate_merge(action: ProposeMerge, db: Database) -> ValidationResult:
    if not action.keep_label or not action.drop_label:
        return ValidationResult(False, "keep_label or drop_label is empty")
    if action.keep_label == action.drop_label:
        return ValidationResult(False, "keep and drop are the same label")
    entries = OntologyEntryRepository(db)
    if entries.get(action.keep_label) is None:
        return ValidationResult(
            False, f"keep label {action.keep_label!r} does not exist"
        )
    if entries.get(action.drop_label) is None:
        return ValidationResult(
            False, f"drop label {action.drop_label!r} does not exist"
        )
    return ValidationResult(True, None)


def _validate_split(action: ProposeSplit, db: Database) -> ValidationResult:
    if not action.label:
        return ValidationResult(False, "label is empty")
    entries = OntologyEntryRepository(db)
    if entries.get(action.label) is None:
        return ValidationResult(
            False, f"label {action.label!r} does not exist"
        )
    if len(action.into) < 2:
        return ValidationResult(False, "split must produce at least 2 variants")
    return ValidationResult(True, None)


# --- Application ------------------------------------------------------------


def apply(action: Action, db: Database) -> dict[str, Any]:
    if isinstance(action, ProposeParent):
        return _apply_parent(action, db)
    if isinstance(action, ProposeAlias):
        return _apply_alias(action, db)
    raise NotImplementedError(
        f"Cannot auto-apply {action.action_type}; destructive actions "
        "must be applied via operator review."
    )


def _apply_parent(action: ProposeParent, db: Database) -> dict[str, Any]:
    entries = OntologyEntryRepository(db)
    child = entries.get(action.child_label)
    previous_parent_label = child.parent_label if child else None
    reparenting = (
        previous_parent_label is not None
        and previous_parent_label != action.parent_label
    )

    # Re-parenting: drop the old tree edge before inserting the new one,
    # otherwise the unique (parent_label, child_label) index is fine but
    # the OLD edge would dangle.
    if reparenting:
        db.ontology_tree.delete_one(
            {
                "parent_label": previous_parent_label,
                "child_label": action.child_label,
            }
        )

    OntologyTreeRepository(db).add_edge(
        OntologyTreeEdge(
            parent_label=action.parent_label,
            child_label=action.child_label,
            relation_type="child_of",
        )
    )
    db.ontology_entries.update_one(
        {"_id": action.child_label},
        {
            "$set": {
                "parent_label": action.parent_label,
                "updated_at": _utcnow(),
            }
        },
    )
    # A parent change is a structural change; entries that referenced
    # the child may need to re-evaluate against its new ancestor chain.
    if reparenting:
        from mahalath.staleness import mark_dependents_stale
        mark_dependents_stale(
            db, action.child_label,
            change_type="reparented",
            note=(
                f"parent changed from {previous_parent_label} to "
                f"{action.parent_label}"
            ),
        )

    return {
        "tree_edge_added": True,
        "parent_label_updated": True,
        "child_label": action.child_label,
        "parent_label": action.parent_label,
        "previous_parent_label": previous_parent_label,
        "reparenting": reparenting,
    }


def _apply_alias(action: ProposeAlias, db: Database) -> dict[str, Any]:
    alias_clean = action.alias.strip()
    db.ontology_entries.update_one(
        {"_id": action.label},
        {
            "$addToSet": {"aliases": alias_clean},
            "$set": {"updated_at": _utcnow()},
        },
    )
    return {"alias_added": alias_clean, "label": action.label}


# --- Dispatcher -------------------------------------------------------------


def dispatch(
    action: Action,
    db: Database,
    *,
    auto_apply_threshold: float = DEFAULT_AUTO_APPLY_THRESHOLD,
    reparent_threshold: float = DEFAULT_REPARENT_THRESHOLD,
) -> DispatchResult:
    """Validate, persist, and (where allowed) apply a proposed action.

    Always writes an ActionProposal regardless of outcome so the audit
    trail captures invalid / pending / applied cases uniformly.

    Threshold selection:
      - destructive actions (merge, split): always pending_review
      - re-parenting (propose_parent where child already has a different
        parent): requires confidence >= reparent_threshold (8.5 default)
      - everything else: requires confidence >= auto_apply_threshold
        (8.0 default)
    """
    proposal = ActionProposal(
        action_type=action.action_type,
        payload=action.payload(),
        reason=action.reason,
        confidence=action.confidence,
        proposed_by=action.proposed_by,
        source_decision_log_id=action.source_decision_log_id,
        source_ontology_review_id=action.source_ontology_review_id,
    )
    repo = ActionProposalRepository(db)

    validation = validate(action, db)
    if not validation.valid:
        proposal.status = "invalid"
        proposal.rejection_reason = validation.reason
        repo.insert(proposal)
        return DispatchResult(
            proposal_id=proposal.proposal_id,
            action_type=action.action_type,
            status="invalid",
            detail=validation.reason or "validation failed",
            payload=action.payload(),
        )

    if action.action_type in DESTRUCTIVE_TYPES:
        proposal.status = "pending_review"
        proposal.rejection_reason = (
            "destructive actions require operator review"
        )
        repo.insert(proposal)
        return DispatchResult(
            proposal_id=proposal.proposal_id,
            action_type=action.action_type,
            status="pending_review",
            detail="destructive action routed to review queue",
            payload=action.payload(),
        )

    effective_threshold = auto_apply_threshold
    threshold_label = "auto-apply"
    if isinstance(action, ProposeParent) and is_reparenting(action, db):
        effective_threshold = reparent_threshold
        threshold_label = "re-parenting"

    if action.confidence < effective_threshold:
        proposal.status = "pending_review"
        proposal.rejection_reason = (
            f"confidence {action.confidence} below {threshold_label} "
            f"threshold {effective_threshold}"
        )
        repo.insert(proposal)
        return DispatchResult(
            proposal_id=proposal.proposal_id,
            action_type=action.action_type,
            status="pending_review",
            detail=(
                f"confidence {action.confidence} below {threshold_label} "
                f"threshold {effective_threshold}"
            ),
            payload=action.payload(),
        )

    try:
        application_result = apply(action, db)
    except Exception as exc:  # pragma: no cover - guards against future drift
        proposal.status = "invalid"
        proposal.rejection_reason = f"apply failed: {exc}"
        repo.insert(proposal)
        return DispatchResult(
            proposal_id=proposal.proposal_id,
            action_type=action.action_type,
            status="invalid",
            detail=f"apply failed: {exc}",
            payload=action.payload(),
        )

    proposal.status = "applied"
    proposal.applied_at = _utcnow()
    proposal.application_result = application_result
    repo.insert(proposal)
    return DispatchResult(
        proposal_id=proposal.proposal_id,
        action_type=action.action_type,
        status="applied",
        detail="auto-applied",
        payload=action.payload(),
    )
