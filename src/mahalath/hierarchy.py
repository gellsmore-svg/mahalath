"""Hierarchy review: show the agent the existing ontology and ask for structural actions.

`run_hierarchy_review` is the seam between the local model and the
action dispatcher. It

  1. snapshots the existing ontology (labels + canonical terms + short
     definition + parent relationships),
  2. presents the new (focus) entry alongside,
  3. asks the model to propose structural actions (propose_parent /
     propose_alias / propose_merge / propose_split),
  4. parses the JSON response into `Action` records,
  5. persists an `OntologyReview` row with the full prompt+response so
     the audit trail is reconstructible,
  6. returns the actions for the caller (typically `process-document`)
     to dispatch.

Calling this DOES NOT apply the actions; that is the dispatcher's job
in `mahalath.actions.dispatch`. Decoupling lets the caller decide on
review-time thresholds and routing without rebuilding the prompt.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import uuid4

from pymongo.database import Database

from mahalath.actions import Action, parse_actions
from mahalath.adapters.base import Adapter, AdapterError
from mahalath.config import RuntimeConfig
from mahalath.db.models import OntologyEntry, OntologyReview
from mahalath.db.repositories import (
    OntologyEntryRepository,
    OntologyReviewRepository,
)

DEFINITION_SNIPPET_CHARS = 280
PROPOSED_BY = "hierarchy_review"


class HierarchyReviewError(Exception):
    """Raised when a review pass cannot complete (missing focus entry, etc.)."""


@dataclass
class HierarchyReviewResult:
    review_id: str
    focus_label: str
    actions: list[Action] = field(default_factory=list)
    no_actions_reason: str | None = None
    raw_response: str = ""
    duration_ms: int = 0


@dataclass
class ConsensusHierarchyReviewResult:
    """Result of N independent hierarchy review passes aggregated by consensus.

    `consensus_actions` are the actions proposed identically by every
    pass (unanimous agreement on action type and exact payload). Their
    confidence is the minimum across the passes that proposed them —
    the most pessimistic agent governs, matching the Stage 1 debate
    aggregation policy (DQ-003).

    `per_pass_actions` keeps each pass's raw output for diagnostics
    when consensus fails.
    """

    focus_label: str
    review_ids: list[str] = field(default_factory=list)
    consensus_actions: list[Action] = field(default_factory=list)
    per_pass_actions: list[list[Action]] = field(default_factory=list)
    total_duration_ms: int = 0
    n_passes: int = 1


def run_hierarchy_review(
    focus_label: str,
    db: Database,
    adapter: Adapter,
    runtime: RuntimeConfig,
    *,
    triggered_by: str = "post_accept",
    source_decision_log_id: str | None = None,
    max_snapshot_entries: int = 50,
    style_overlay: str | None = None,
) -> HierarchyReviewResult:
    entries = OntologyEntryRepository(db)
    focus = entries.get(focus_label)
    if focus is None:
        raise HierarchyReviewError(
            f"focus label {focus_label!r} not found in ontology_entries"
        )

    snapshot = _snapshot(entries, focus_label, limit=max_snapshot_entries)
    prompt = build_review_prompt(snapshot, focus, style_overlay=style_overlay)

    start = time.monotonic()
    try:
        response = adapter.generate(
            prompt, want_json=True, model=runtime.model
        )
    except AdapterError as exc:
        raise HierarchyReviewError(
            f"adapter failed during hierarchy review of {focus_label!r}: {exc}"
        ) from exc
    duration_ms = int((time.monotonic() - start) * 1000)

    review_id = str(uuid4())
    response_json = _extract_json_object(response.text)
    actions = parse_actions(
        response_json,
        proposed_by=PROPOSED_BY,
        source_decision_log_id=source_decision_log_id,
        source_ontology_review_id=review_id,
    )

    no_actions_reason_raw = response_json.get("no_actions_reason")
    no_actions_reason = (
        str(no_actions_reason_raw).strip()
        if isinstance(no_actions_reason_raw, str) and no_actions_reason_raw.strip()
        else None
    )

    review_record = OntologyReview(
        review_id=review_id,
        triggered_by=triggered_by,
        focus_mpl_label=focus_label,
        source_decision_log_id=source_decision_log_id,
        model=response.model,
        prompt=prompt,
        response=response.text,
        duration_ms=duration_ms,
        actions_count=len(actions),
        no_actions_reason=no_actions_reason,
    )
    OntologyReviewRepository(db).insert(review_record)

    return HierarchyReviewResult(
        review_id=review_id,
        focus_label=focus_label,
        actions=actions,
        no_actions_reason=no_actions_reason,
        raw_response=response.text,
        duration_ms=duration_ms,
    )


def run_hierarchy_review_consensus(
    focus_label: str,
    db: Database,
    adapter: Adapter,
    runtime: RuntimeConfig,
    *,
    n_passes: int | None = None,
    triggered_by: str = "post_accept",
    source_decision_log_id: str | None = None,
    style_overlay: str | None = None,
) -> ConsensusHierarchyReviewResult:
    """Run hierarchy review N times; keep only unanimously-agreed actions.

    Action identity for consensus is (action_type, payload-as-frozen-pairs).
    Two actions with swapped child/parent labels are DIFFERENT proposals
    and will not aggregate — exactly the property we want when the
    small-model failure mode is "right relationship, wrong direction."

    Each pass writes its own OntologyReview row, so the audit trail
    captures all N prompts and responses regardless of whether
    consensus emerges.
    """
    effective_passes = (
        n_passes if n_passes is not None else runtime.hierarchy_consensus_passes
    )
    if effective_passes < 1:
        raise ValueError("n_passes must be >= 1")

    review_ids: list[str] = []
    per_pass_actions: list[list[Action]] = []
    total_duration = 0
    for _ in range(effective_passes):
        result = run_hierarchy_review(
            focus_label, db, adapter, runtime,
            triggered_by=triggered_by,
            source_decision_log_id=source_decision_log_id,
            style_overlay=style_overlay,
        )
        review_ids.append(result.review_id)
        per_pass_actions.append(result.actions)
        total_duration += result.duration_ms

    consensus_actions = _consensus(per_pass_actions, effective_passes)

    return ConsensusHierarchyReviewResult(
        focus_label=focus_label,
        review_ids=review_ids,
        consensus_actions=consensus_actions,
        per_pass_actions=per_pass_actions,
        total_duration_ms=total_duration,
        n_passes=effective_passes,
    )


def _consensus(per_pass: list[list[Action]], n_passes: int) -> list[Action]:
    """Return actions proposed identically in every pass.

    Within-pass duplicates (same key proposed twice in a single pass)
    are deduped before counting, so an over-eager pass cannot push an
    action over the threshold on its own.
    """
    if n_passes <= 1:
        # No consensus to compute; flatten and return.
        return [a for pass_actions in per_pass for a in pass_actions]

    occurrences: dict[tuple, list[Action]] = defaultdict(list)
    for pass_actions in per_pass:
        seen_in_pass: set[tuple] = set()
        for action in pass_actions:
            key = _action_key(action)
            if key in seen_in_pass:
                continue
            seen_in_pass.add(key)
            occurrences[key].append(action)

    consensus: list[Action] = []
    for key, actions in occurrences.items():
        if len(actions) < n_passes:
            continue
        # Use the last pass's action as the representative; replace
        # confidence with the minimum across passes (DQ-003 aggregation).
        representative = actions[-1]
        min_confidence = min(a.confidence for a in actions)
        consensus.append(replace(representative, confidence=min_confidence))
    return consensus


def _action_key(action: Action) -> tuple:
    payload_items = tuple(
        sorted((k, _hashable(v)) for k, v in action.payload().items())
    )
    return (action.action_type, payload_items)


def _hashable(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(value)
    if isinstance(value, dict):
        return tuple(sorted(value.items()))
    return value


# --- Snapshot + prompt -----------------------------------------------------


def _snapshot(
    entries: OntologyEntryRepository, focus_label: str, *, limit: int
) -> list[OntologyEntry]:
    snapshot: list[OntologyEntry] = []
    for label in sorted(entries.all_labels()):
        if label == focus_label:
            continue
        entry = entries.get(label)
        if entry is None:
            continue
        snapshot.append(entry)
        if len(snapshot) >= limit:
            break
    return snapshot


def build_review_prompt(
    snapshot: list[OntologyEntry],
    focus: OntologyEntry,
    *,
    style_overlay: str | None = None,
) -> str:
    from mahalath.style import render_style_block

    lines: list[str] = []
    lines.append("Existing ontology entries:")
    lines.append("")
    if not snapshot:
        lines.append("(none — this is the first entry in the ontology)")
        lines.append("")
    else:
        for entry in snapshot:
            lines.extend(_render_entry(entry))
            lines.append("")

    lines.append("NEW entry being reviewed:")
    lines.append("")
    lines.extend(_render_entry(focus))
    lines.append("")

    style_block = render_style_block(style_overlay)
    if style_block:
        lines.append(style_block)
        lines.append("")

    lines.append(_TASK_INSTRUCTIONS)
    return "\n".join(lines)


def _render_entry(entry: OntologyEntry) -> list[str]:
    placement = (
        f"child of {entry.parent_label}"
        if entry.parent_label
        else "top-level"
    )
    head = f"{entry.mpl_label}: {entry.canonical_term}  ({placement})"
    lines = [head]
    if entry.aliases:
        lines.append(f"  aliases: {', '.join(entry.aliases)}")
    snippet = _short_definition(entry)
    if snippet:
        lines.append(f"  {snippet}")
    return lines


def _short_definition(entry: OntologyEntry) -> str:
    if not entry.definitions:
        return ""
    text = entry.definitions[0].text.strip()
    if len(text) > DEFINITION_SNIPPET_CHARS:
        return text[:DEFINITION_SNIPPET_CHARS].rstrip() + "..."
    return text


_TASK_INSTRUCTIONS = """\
Task:
You are an ontology curator. Look at the existing entries above and the NEW entry being reviewed. Propose any structural relationships that should be created.

CRITICAL: every label field in your actions MUST be the MPL identifier string from the entries above (e.g. "MPL-001", "MPL-002.003a"). NEVER use the canonical term or any other identifier — only the exact MPL-NNN string. If you cannot find the exact MPL string in the entries above, do not propose that action.

Available action types:
  - propose_parent: an existing label should become a child of another existing label. Required fields: child_label, parent_label (both MPL identifiers). This applies BOTH to initial parent assignment (the child is currently top-level) AND to re-parenting (the child already has a parent but the proposed parent better captures the relationship). Re-parenting requires confidence >= 8.5 to auto-apply.
  - propose_alias: add a synonym to an existing label. Required fields: label (MPL identifier), alias (free-form term string).
  - propose_merge: two labels refer to the same concept; one should be marked superseded by the other. Required fields: keep_label, drop_label (both MPL identifiers).
  - propose_split: a label needs to be split into contradistinct variants. Required fields: label (MPL identifier), into (list of new term strings).

Each action must include:
  reason: one sentence explaining the structural relationship.
  confidence: number 0.0 to 10.0. Confidence below 8.0 is routed to operator review. Destructive actions (merge, split) are ALWAYS routed to operator review regardless of confidence.

Example of a correctly-formatted parent action:
  {"type": "propose_parent", "child_label": "MPL-002", "parent_label": "MPL-001", "reason": "MPL-002 is a specific variant of the more general MPL-001.", "confidence": 8.5}

BE CONSERVATIVE. It is much better to propose nothing than to propose a wrong relationship. If you are unsure whether two entries are parent/child or just neighbours, do NOT propose. Only propose a parent relationship when the evidence in the definitions is clear.

If no clear structural action is warranted, return actions: [] and explain in no_actions_reason.

Output ONLY a JSON object of this exact shape:
{
  "actions": [
    {"type": "<action_type>", ...required fields..., "reason": "<one sentence>", "confidence": <0.0-10.0>}
  ],
  "no_actions_reason": <null if you proposed actions, or a string if actions is empty>
}

No preamble, no Markdown, no commentary outside the JSON.
"""


def _extract_json_object(text: str) -> dict[str, Any]:
    """Find and parse the first balanced JSON object in `text`.

    Tolerant of small-model preambles. Same logic as
    `mahalath.extraction._extract_json_object`; duplicated rather than
    imported to keep each call site's error type local. Worth a future
    refactor into `mahalath.json_extract`.
    """
    text = text.strip()
    if not text:
        return {}

    try:
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            return loaded
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    if start == -1:
        return {}

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    loaded = json.loads(text[start : i + 1])
                    if isinstance(loaded, dict):
                        return loaded
                except json.JSONDecodeError:
                    return {}
    return {}
