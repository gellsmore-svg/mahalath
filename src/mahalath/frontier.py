"""Frontier-model review pass over the pending_review queue.

Given a more capable adapter (Claude API by default), drain the
pending_review queue by adjudicating each item with full corpus
context: child + parent definitions in full, the style overlay, and
the original proposing model's reasoning.

Decisions feed back through the existing accept_proposal /
reject_proposal flows so the audit chain stays uniform:

    gemma4:e2b proposes
      → routes to pending_review (confidence below threshold)
      → frontier_review reads the queue
      → builds a context-rich prompt with both entries' definitions
      → calls the frontier adapter (Claude Opus 4.7 by default)
      → parses {decision, confidence, reasoning}
      → ACCEPT  → accept_proposal(...) — operator_note records the chain
        REJECT  → reject_proposal(...)
        ESCALATE → leaves in pending_review with a frontier-escalated note,
                   surfacing the truly-ambiguous cases for human review

The escalate branch is the seam for the chat-interface architecture
the operator described: once the human surface becomes conversational
rather than label-browsing, escalated items are what the chat is for.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from pymongo.database import Database

from mahalath.adapters.base import Adapter, AdapterError
from mahalath.config import AppConfig
from mahalath.db.models import ActionProposal
from mahalath.db.repositories import (
    ActionProposalRepository,
    OntologyEntryRepository,
)
from mahalath.proposals import (
    ProposalError,
    accept_proposal,
    reject_proposal,
)
from mahalath.style import load_style_overlay


log = logging.getLogger("mahalath.frontier")


class FrontierReviewError(Exception):
    """Raised when a verdict cannot be parsed from the adapter response."""


@dataclass(frozen=True)
class FrontierVerdict:
    decision: str  # accept | reject | escalate
    confidence: float
    reasoning: str


@dataclass
class FrontierReviewResult:
    items_in_queue_at_start: int = 0
    items_reviewed: int = 0
    items_accepted: int = 0
    items_rejected: int = 0
    items_escalated: int = 0
    items_errored: int = 0
    errors: list[str] = field(default_factory=list)
    verdicts: list[dict[str, Any]] = field(default_factory=list)


def frontier_review(
    config: AppConfig,
    db: Database,
    adapter: Adapter,
    *,
    max_items: int = 25,
) -> FrontierReviewResult:
    """Drain pending_review by adjudicating each item with the frontier adapter.

    `max_items` caps the model spend per run; the next call picks up
    where this one left off because still-pending items remain in the
    queue.
    """
    proposals_repo = ActionProposalRepository(db)
    pending = proposals_repo.by_status("pending_review")
    style_overlay = load_style_overlay(config)

    result = FrontierReviewResult(items_in_queue_at_start=len(pending))

    for proposal in pending[:max_items]:
        try:
            prompt = build_review_prompt(proposal, db, style_overlay, proposing_model=config.runtime.model)
            response = adapter.generate(prompt, want_json=True)
            verdict = parse_verdict(response.text)
        except (AdapterError, FrontierReviewError) as exc:
            result.items_errored += 1
            result.errors.append(f"{proposal.proposal_id[:8]}: {exc}")
            continue

        result.items_reviewed += 1
        note = (
            f"frontier-review ({verdict.confidence:.1f}): {verdict.reasoning}"
        )
        result.verdicts.append({
            "proposal_id": proposal.proposal_id,
            "child_label": proposal.payload.get("child_label"),
            "parent_label": proposal.payload.get("parent_label"),
            "decision": verdict.decision,
            "confidence": verdict.confidence,
            "reasoning": verdict.reasoning,
        })

        try:
            if verdict.decision == "accept":
                accept_proposal(proposal.proposal_id, db, note=note)
                result.items_accepted += 1
                log.info(
                    "frontier: accepted %s (%s → %s, conf %.1f)",
                    proposal.proposal_id[:8],
                    proposal.payload.get("child_label"),
                    proposal.payload.get("parent_label"),
                    verdict.confidence,
                )
            elif verdict.decision == "reject":
                reject_proposal(proposal.proposal_id, db, note=note)
                result.items_rejected += 1
                log.info(
                    "frontier: rejected %s (%s → %s, conf %.1f)",
                    proposal.proposal_id[:8],
                    proposal.payload.get("child_label"),
                    proposal.payload.get("parent_label"),
                    verdict.confidence,
                )
            else:
                # ESCALATE: leave in pending_review, but record the frontier's
                # note so a future chat-interface surface can show the human
                # what the frontier model couldn't resolve.
                db.action_proposals.update_one(
                    {"proposal_id": proposal.proposal_id},
                    {"$set": {"operator_note": f"frontier-escalated: {verdict.reasoning}"}},
                )
                result.items_escalated += 1
                log.info(
                    "frontier: escalated %s (%s → %s)",
                    proposal.proposal_id[:8],
                    proposal.payload.get("child_label"),
                    proposal.payload.get("parent_label"),
                )
        except ProposalError as exc:
            # Race: the proposal status changed under us (e.g., human
            # accepted it in the UI between our query and our dispatch).
            result.items_errored += 1
            result.errors.append(f"{proposal.proposal_id[:8]}: {exc}")

    return result


# --- Prompt construction ---------------------------------------------------


def build_review_prompt(
    proposal: ActionProposal,
    db: Database,
    style_overlay: str | None,
    proposing_model: str = "the configured local model",
) -> str:
    """Build a context-rich prompt with both entries' definitions."""
    entries_repo = OntologyEntryRepository(db)
    payload = proposal.payload

    parts: list[str] = []
    parts.append("You are a frontier reviewer for the Mahalath ontology builder.")
    parts.append("")
    parts.append("CONTEXT")
    parts.append(
        f"A local model ({proposing_model}) proposed the structural action below "
        "at a confidence that fell below the auto-apply threshold and got "
        "routed to operator review. Your job is to adjudicate with full "
        "definitional context — something the local model lacks."
    )
    parts.append("")
    parts.append(f"ACTION TYPE: {proposal.action_type}")
    parts.append(f"PROPOSING MODEL: {proposing_model}")
    parts.append(f"PROPOSING MODEL CONFIDENCE: {proposal.confidence}")
    parts.append(f"PROPOSING MODEL REASONING: {proposal.reason}")
    parts.append("")

    if proposal.action_type == "propose_parent":
        child_label = payload.get("child_label", "")
        parent_label = payload.get("parent_label", "")
        child = entries_repo.get(child_label)
        parent = entries_repo.get(parent_label)

        parts.append(
            f"PROPOSED EDGE: {child_label} (child) → {parent_label} (parent)"
        )
        parts.append("")
        parts.append("CHILD ENTRY")
        parts.append(_render_entry(child_label, child))
        parts.append("")
        parts.append("PARENT ENTRY")
        parts.append(_render_entry(parent_label, parent))
        parts.append("")
    else:
        parts.append(f"PAYLOAD: {json.dumps(payload)}")
        parts.append("")

    if style_overlay:
        parts.append("STYLE GUIDANCE FOR THIS CORPUS")
        parts.append(style_overlay.strip())
        parts.append("")

    parts.append("YOUR TASK")
    parts.append(
        "Evaluate whether the proposed parent edge accurately reflects the "
        "relationship between the child and parent as captured by their "
        "definitions. Heuristic: the parent is the term whose definition "
        "could meaningfully substitute for the child in a more general "
        "context, but not vice versa. (Equivalently: the child is a "
        "specialisation of the parent.)"
    )
    parts.append("")
    parts.append("Decide one of:")
    parts.append(
        "  ACCEPT   — the edge is correct as proposed."
    )
    parts.append(
        "  REJECT   — the edge is wrong (backwards, no real hierarchical "
        "relationship, or the two should be siblings under a different parent)."
    )
    parts.append(
        "  ESCALATE — a true frontier case where corpus expertise that isn't "
        "in the definitions is required. The operator will see escalated "
        "items in a chat surface; tell them what's contested and why."
    )
    parts.append("")
    parts.append(
        'Output ONLY a JSON object: '
        '{"decision": "accept" | "reject" | "escalate", '
        '"confidence": <number 0.0-10.0>, '
        '"reasoning": "<one or two sentences explaining your verdict>"}'
    )
    parts.append("No preamble, no markdown fences.")

    return "\n".join(parts)


def _render_entry(label: str, entry: Any) -> str:
    if entry is None:
        return f"  Label: {label}\n  (entry not found in ontology)"
    lines = [
        f"  Label: {label}",
        f"  Term:  {entry.canonical_term!r}",
    ]
    if getattr(entry, "parent_label", None):
        lines.append(f"  Current parent: {entry.parent_label}")
    if getattr(entry, "aliases", None):
        lines.append(f"  Aliases: {', '.join(entry.aliases)}")
    defs = getattr(entry, "definitions", None) or []
    if defs:
        for i, d in enumerate(defs, 1):
            attribution = getattr(d, "model_used", None) or "?"
            text = getattr(d, "text", "")
            lines.append(f"  Definition #{i} (from {attribution}):")
            lines.append(f"    {text}")
    else:
        lines.append("  (no definitions recorded)")
    return "\n".join(lines)


# --- Verdict parsing -------------------------------------------------------


def parse_verdict(response_text: str) -> FrontierVerdict:
    """Parse the frontier adapter's JSON response into a FrontierVerdict.

    Tolerates leading/trailing prose around the JSON object the way the
    extraction and debate parsers do — frontier models occasionally
    wrap output in fences despite the instruction.
    """
    text = response_text.strip()
    if not text:
        raise FrontierReviewError("frontier adapter returned empty response")

    # Strip common Markdown JSON fences.
    if text.startswith("```"):
        text = text.split("```", 2)
        if len(text) >= 2:
            inner = text[1]
            if inner.startswith("json"):
                inner = inner[4:].lstrip()
            text = inner
        else:
            text = "".join(text)

    obj: Any
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < 0 or end <= start:
            raise FrontierReviewError(
                f"no JSON object found in frontier response: {text[:200]!r}"
            )
        try:
            obj = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise FrontierReviewError(
                f"JSON parse failed: {exc}; raw: {text[:200]!r}"
            ) from exc

    if not isinstance(obj, dict):
        raise FrontierReviewError(
            f"frontier response is not a JSON object: {type(obj).__name__}"
        )

    decision = str(obj.get("decision", "")).strip().lower()
    if decision not in {"accept", "reject", "escalate"}:
        raise FrontierReviewError(
            f"invalid decision value: {decision!r} "
            "(expected accept | reject | escalate)"
        )

    try:
        confidence = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence != confidence:  # NaN guard
        confidence = 0.0
    confidence = max(0.0, min(10.0, confidence))

    reasoning = str(obj.get("reasoning", "")).strip()
    if not reasoning:
        reasoning = "(no reasoning provided)"

    return FrontierVerdict(
        decision=decision, confidence=confidence, reasoning=reasoning
    )
