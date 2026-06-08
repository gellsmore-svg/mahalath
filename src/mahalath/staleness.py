"""Reference tracking + staleness flagging for the ontology graph.

When an entry's definition is written it records which other MPL
labels it explicitly references (regex on `MPL-NNN[.NNN…][a-z]?`)
plus semantic-substring matches against the live ontology index.
That gives the database a reverse-index: "who depends on MPL-X?".
When MPL-X is later modified (definition changed, parent re-pointed,
rollback) the dependents are flagged stale. Already-stale entries
receive a new reason appended but stay flagged.

Four things live here:

  Reference extraction.
      `extract_references(text)` regexes label tokens out of a single
      definition text. `extract_semantic_references` matches canonical
      terms with word-boundary regex. `compute_references_for_entry`
      merges both, excludes self.

  Invalidation.
      `mark_dependents_stale(db, upstream_label, ...)` walks the
      reverse-index and flags every entry that references the
      upstream. Cascades by default; cycle-safe via the `visited` set.

  Resolution.
      `clear_stale(db, label)` is the unflag path. The REM stale-audit
      job (`audit_pending_stale`) calls it when the model confirms
      the dependent's definition is still consistent with current
      upstream state.

  Audit pass.
      `audit_pending_stale(config, db, adapter, ...)` walks `list_stale`,
      asks an adapter (gemma4:e2b cheap pass, claude_api for higher
      stakes) to read each stale entry alongside its referenced
      upstreams' current state, and either clears or appends a
      verdict. Wires into the scheduler's REM job to complete the
      self-healing loop.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pymongo.database import Database

from mahalath.adapters.base import Adapter, AdapterError
from mahalath.config import AppConfig
from mahalath.db.models import OntologyEntry
from mahalath.db.repositories import OntologyEntryRepository
from mahalath.style import load_style_overlay, render_style_block


log = logging.getLogger("mahalath.staleness")


_MPL_LABEL_PATTERN = re.compile(r"\bMPL-\d{3}(?:\.\d{3})*[a-z]?\b")
# Minimum canonical-term length to consider for semantic matching.
# Single common words ("Form", "Time") would over-match; the threshold
# trades recall for precision.
_MIN_SEMANTIC_TERM_LEN = 4


# --- Extraction -----------------------------------------------------------


def extract_references(text: str) -> list[str]:
    """Return MPL label tokens found in `text`, in order, deduped.

    Catches only explicit `MPL-NNN…` mentions. For canonical-term
    matches see `extract_semantic_references`.
    """
    if not text:
        return []
    return list(dict.fromkeys(_MPL_LABEL_PATTERN.findall(text)))


def extract_semantic_references(
    text: str,
    ontology_index: dict[str, str],
    *,
    self_label: str | None = None,
) -> list[str]:
    """Return MPL labels whose canonical_term appears as a word in `text`.

    `ontology_index` is a {case-folded canonical_term → MPL label} dict;
    build it once per pass via `build_ontology_index(db)`.

    Word-boundary regex is used so "Substrate" matches in "the substrate is..."
    AND in "Relational Substrate" (the trailing space is a word boundary).
    That's intentional: a definition mentioning "Relational Substrate"
    DOES reference both the RS entry and the Substrate entry.
    """
    if not text or not ontology_index:
        return []
    seen: dict[str, None] = {}
    text_cf = text.casefold()
    for term_cf, label in ontology_index.items():
        if label == self_label:
            continue
        if len(term_cf) < _MIN_SEMANTIC_TERM_LEN:
            continue
        pattern = r"\b" + re.escape(term_cf) + r"\b"
        if re.search(pattern, text_cf):
            seen[label] = None
    return list(seen.keys())


def build_ontology_index(db: Database) -> dict[str, str]:
    """Build a {case-folded canonical_term → MPL label} index.

    Snapshot of the current ontology — pass to `compute_references_for_entry`
    or `update_references` to enable semantic matching.
    """
    return {
        doc["canonical_term"].casefold(): doc["_id"]
        for doc in db.ontology_entries.find(
            {}, {"canonical_term": 1, "_id": 1}
        )
        if doc.get("canonical_term")
    }


def compute_references_for_entry(
    entry: OntologyEntry,
    *,
    ontology_index: dict[str, str] | None = None,
) -> list[str]:
    """Combine references from all definitions, excluding the entry's own label.

    If `ontology_index` is given, semantic references (canonical-term
    mentions of OTHER entries) are merged with the explicit MPL-label
    matches.
    """
    seen: dict[str, None] = {}
    for d in entry.definitions:
        for ref in extract_references(d.text):
            if ref != entry.mpl_label:
                seen.setdefault(ref, None)
        if ontology_index:
            for ref in extract_semantic_references(
                d.text, ontology_index, self_label=entry.mpl_label
            ):
                seen.setdefault(ref, None)
    return list(seen.keys())


def update_references(
    db: Database,
    mpl_label: str,
    *,
    ontology_index: dict[str, str] | None = None,
) -> list[str]:
    """Recompute references_labels for one entry; persist if changed.

    Builds an `ontology_index` snapshot if none is given — pass one
    explicitly for batch operations (`backfill_references` does this).
    """
    repo = OntologyEntryRepository(db)
    entry = repo.get(mpl_label)
    if entry is None:
        return []
    if ontology_index is None:
        ontology_index = build_ontology_index(db)
    refs = compute_references_for_entry(entry, ontology_index=ontology_index)
    if refs != entry.references_labels:
        db.ontology_entries.update_one(
            {"_id": mpl_label},
            {"$set": {"references_labels": refs, "updated_at": _utcnow()}},
        )
    return refs


# --- Reverse-index queries ------------------------------------------------


def entries_referencing(db: Database, label: str) -> list[OntologyEntry]:
    """Return all entries whose references_labels contains `label`."""
    cursor = db.ontology_entries.find({"references_labels": label})
    return [OntologyEntry.model_validate(doc) for doc in cursor]


def list_stale(db: Database, *, limit: int = 100) -> list[OntologyEntry]:
    cursor = (
        db.ontology_entries.find({"is_stale": True})
        .sort("updated_at", -1)
        .limit(limit)
    )
    return [OntologyEntry.model_validate(doc) for doc in cursor]


# --- Mutation -------------------------------------------------------------


def mark_dependents_stale(
    db: Database,
    upstream_label: str,
    *,
    change_type: str,
    note: str | None = None,
    cascade: bool = True,
    visited: set[str] | None = None,
) -> list[str]:
    """Mark all entries referencing `upstream_label` as stale.

    Returns the list of MPL labels that newly became stale (entries
    that were already stale receive a new reason appended but are not
    included in the return list).

    Cascading: a newly-stale entry propagates to its own dependents,
    cycle-safe via the `visited` set.
    """
    if visited is None:
        visited = set()
    if upstream_label in visited:
        return []
    visited.add(upstream_label)

    reason = {
        "upstream_label": upstream_label,
        "change_type": change_type,
        "changed_at": _utcnow(),
        "note": note,
    }

    affected: list[str] = []
    for entry in entries_referencing(db, upstream_label):
        was_stale = entry.is_stale
        db.ontology_entries.update_one(
            {"_id": entry.mpl_label},
            {
                "$set": {"is_stale": True, "updated_at": _utcnow()},
                "$push": {"stale_reasons": reason},
            },
        )
        if not was_stale:
            affected.append(entry.mpl_label)
        if cascade:
            affected.extend(mark_dependents_stale(
                db, entry.mpl_label,
                change_type=f"cascade_from_{change_type}",
                note=f"cascaded via {upstream_label}",
                cascade=cascade,
                visited=visited,
            ))
    return affected


def clear_stale(db: Database, mpl_label: str) -> None:
    """Unflag a previously-stale entry (after successful re-debate)."""
    db.ontology_entries.update_one(
        {"_id": mpl_label},
        {
            "$set": {
                "is_stale": False,
                "stale_reasons": [],
                "updated_at": _utcnow(),
            }
        },
    )


# --- Operator-facing helpers ----------------------------------------------


def append_operator_definition(
    db: Database,
    mpl_label: str,
    text: str,
    *,
    language: str = "en",
    context_id: str | None = None,
    cascade: bool = True,
    note: str | None = None,
) -> None:
    """Append an operator-authored definition AND propagate staleness.

    Per the polysemy design (S2.22), `context_id` tags the frame this
    definition speaks within. Multiple definitions with DIFFERENT
    contexts are co-equal — appending one does NOT supersede others
    in different contexts. Appending one in the SAME context (and
    same model_used) still marks dependents stale because the
    definitional content for that frame has changed.

    This is the canonical way to add an operator definition (replaces
    direct `$push: definitions` patterns). It:
      - appends the definition (with optional context_id)
      - recomputes references_labels for this entry
      - marks dependents stale because this entry's definitional
        content just changed
    """
    now = _utcnow()
    db.ontology_entries.update_one(
        {"_id": mpl_label},
        {
            "$push": {
                "definitions": {
                    "text": text,
                    "language": language,
                    "model_used": "operator",
                    "decision_log_id": None,
                    "context_id": context_id,
                    "created_at": now,
                }
            },
            "$set": {"updated_at": now},
        },
    )
    update_references(db, mpl_label)
    mark_dependents_stale(
        db, mpl_label,
        change_type="definition_updated",
        note=note or "operator-authored definition appended",
        cascade=cascade,
    )


def backfill_references(db: Database) -> dict[str, int]:
    """Recompute references_labels for every entry in the database.

    One-shot migration helper for databases populated before the
    S2.17 schema. Uses semantic matching (canonical-term substring)
    in addition to explicit MPL-label regex, so existing data
    populated by agents that write in canonical-term form gets
    populated correctly. Returns counts: {scanned, updated, total_refs}.
    """
    scanned = 0
    updated = 0
    total_refs = 0
    repo = OntologyEntryRepository(db)
    ontology_index = build_ontology_index(db)
    for label in repo.all_labels():
        scanned += 1
        entry = repo.get(label)
        if entry is None:
            continue
        new_refs = compute_references_for_entry(
            entry, ontology_index=ontology_index
        )
        total_refs += len(new_refs)
        if new_refs != (entry.references_labels or []):
            db.ontology_entries.update_one(
                {"_id": label},
                {"$set": {"references_labels": new_refs}},
            )
            updated += 1
    return {"scanned": scanned, "updated": updated, "total_refs": total_refs}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _infer_context_name(
    definition_text: str, available_contexts: list[dict]
) -> str | None:
    """Pick the context whose description has the strongest token overlap with
    the definition text. Used as a fallback when the model omits context_name.

    Word-boundary tokens >= 4 chars are scored. Empty overlap → None.
    """
    if not definition_text or not available_contexts:
        return None
    def_tokens = {
        t.lower()
        for t in re.findall(r"\b[A-Za-z]{4,}\b", definition_text)
    }
    best_name: str | None = None
    best_score = 0
    for ctx in available_contexts:
        desc_tokens = {
            t.lower()
            for t in re.findall(r"\b[A-Za-z]{4,}\b", ctx.get("description", ""))
        }
        score = len(def_tokens & desc_tokens)
        if score > best_score:
            best_score = score
            best_name = ctx.get("name")
    return best_name if best_score > 0 else None


# --- Audit pass -----------------------------------------------------------


class AuditError(Exception):
    """Raised when an audit verdict cannot be parsed."""


@dataclass(frozen=True)
class StalenessAuditVerdict:
    decision: str  # consistent | inconsistent | unclear
    confidence: float
    reasoning: str


@dataclass
class StalenessAuditResult:
    items_at_start: int = 0
    items_audited: int = 0
    items_cleared: int = 0
    items_still_stale: int = 0
    items_errored: int = 0
    verdicts: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


_AUDIT_CLEAR_THRESHOLD = 7.0


def build_audit_prompt(
    entry: OntologyEntry,
    db: Database,
    style_overlay: str | None,
    *,
    max_referenced: int = 10,
    max_reasons: int = 5,
) -> str:
    """Build a context-rich consistency-check prompt for a stale entry."""
    entries_repo = OntologyEntryRepository(db)

    parts: list[str] = []
    parts.append("You are an ontology consistency auditor.")
    parts.append("")
    parts.append(
        "The entry below was previously accepted into the ontology. Since "
        "then, one or more entries it references have been changed, and "
        "the system has flagged this entry as potentially stale. Decide "
        "whether the entry's current definition is still consistent with "
        "the current state of its references."
    )
    parts.append("")
    parts.append("STALE ENTRY")
    parts.append(f"  Label: {entry.mpl_label}")
    parts.append(f"  Term:  {entry.canonical_term!r}")
    parts.append(
        f"  Current parent: {entry.parent_label or '(top-level)'}"
    )
    if entry.definitions:
        parts.append("  Definitions (most recent last):")
        for i, d in enumerate(entry.definitions, 1):
            attrib = d.model_used or "?"
            parts.append(f"    {i}. (from {attrib}) {d.text}")
    else:
        parts.append("  (no definitions recorded)")
    parts.append("")

    parts.append("REFERENCED ENTRIES (their CURRENT state)")
    if entry.references_labels:
        for ref_label in entry.references_labels[:max_referenced]:
            ref = entries_repo.get(ref_label)
            if ref is None:
                parts.append(f"  {ref_label}: (no longer exists)")
                continue
            ref_parent = ref.parent_label or "(top-level)"
            parts.append(
                f"  {ref_label} {ref.canonical_term!r}  parent: {ref_parent}"
            )
            if ref.definitions:
                latest = ref.definitions[-1]
                parts.append(
                    f"    Current def ({latest.model_used or '?'}): "
                    f"{latest.text}"
                )
    else:
        parts.append("  (none recorded — semantic refs may have been missed)")
    parts.append("")

    if entry.stale_reasons:
        parts.append("CHANGES THAT FLAGGED THIS ENTRY")
        for r in entry.stale_reasons[-max_reasons:]:
            ts = r.get("changed_at", "")
            change_type = r.get("change_type", "?")
            upstream = r.get("upstream_label", "?")
            note = r.get("note", "")
            parts.append(
                f"  {ts}  {change_type} on {upstream}  ({note})"
            )
        parts.append("")

    if style_overlay:
        block = render_style_block(style_overlay)
        if block:
            parts.append(block)
            parts.append("")

    parts.append("YOUR TASK")
    parts.append(
        "Decide whether the entry's definition is still consistent with the "
        "current state of its references and the corpus framing."
    )
    parts.append("")
    parts.append("  CONSISTENT   — the definition still holds. The upstream changes do not invalidate it.")
    parts.append("  INCONSISTENT — the definition relies on a state of the upstream that no longer matches.")
    parts.append("  UNCLEAR      — genuine ambiguity. Operator review needed.")
    parts.append("")
    parts.append(
        'Output ONLY a JSON object: '
        '{"decision": "consistent" | "inconsistent" | "unclear", '
        '"confidence": <number 0.0-10.0>, '
        '"reasoning": "<one or two sentences>"}'
    )
    parts.append("No preamble, no markdown.")

    return "\n".join(parts)


def parse_audit_verdict(response_text: str) -> StalenessAuditVerdict:
    text = response_text.strip()
    if not text:
        raise AuditError("audit adapter returned empty response")

    if text.startswith("```"):
        parts = text.split("```", 2)
        if len(parts) >= 2:
            inner = parts[1]
            if inner.startswith("json"):
                inner = inner[4:].lstrip()
            text = inner

    try:
        obj: Any = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < 0 or end <= start:
            raise AuditError(
                f"no JSON object found in audit response: {text[:200]!r}"
            )
        try:
            obj = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise AuditError(
                f"JSON parse failed: {exc}; raw: {text[:200]!r}"
            ) from exc

    if not isinstance(obj, dict):
        raise AuditError(
            f"audit response is not a JSON object: {type(obj).__name__}"
        )

    decision = str(obj.get("decision", "")).strip().lower()
    if decision not in {"consistent", "inconsistent", "unclear"}:
        raise AuditError(f"invalid decision value: {decision!r}")

    try:
        confidence = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence != confidence:  # NaN
        confidence = 0.0
    confidence = max(0.0, min(10.0, confidence))

    reasoning = str(obj.get("reasoning", "")).strip()
    if not reasoning:
        reasoning = "(no reasoning provided)"

    return StalenessAuditVerdict(
        decision=decision, confidence=confidence, reasoning=reasoning
    )


def audit_stale_entry(
    entry: OntologyEntry,
    db: Database,
    adapter: Adapter,
    *,
    style_overlay: str | None = None,
) -> StalenessAuditVerdict:
    """Ask `adapter` whether `entry`'s definition is still consistent."""
    prompt = build_audit_prompt(entry, db, style_overlay)
    response = adapter.generate(prompt, want_json=True)
    return parse_audit_verdict(response.text)


class RedefineError(Exception):
    """Raised when a redefine response cannot be parsed."""


@dataclass(frozen=True)
class RedefineVerdict:
    new_definition: str
    confidence: float
    rationale: str
    context_name: str | None = None


@dataclass
class RedefineResult:
    items_at_start: int = 0
    items_redefined: int = 0
    items_skipped: int = 0
    items_errored: int = 0
    verdicts: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def stale_entries_with_inconsistent_audit(
    db: Database, *, limit: int = 50
) -> list[OntologyEntry]:
    """Return stale entries that an audit has called inconsistent (or unclear).

    Filters list_stale to those whose stale_reasons contains at least
    one `audit_inconsistent` or `audit_unclear` entry — i.e., the audit
    pass has already looked at them and decided they need attention,
    not just structurally-flagged-then-untouched items.
    """
    cursor = (
        db.ontology_entries.find({
            "is_stale": True,
            "stale_reasons.change_type": {
                "$in": ["audit_inconsistent", "audit_unclear"]
            },
        })
        .sort("updated_at", 1)  # oldest stale first
        .limit(limit)
    )
    return [OntologyEntry.model_validate(doc) for doc in cursor]


def build_redefine_prompt(
    entry: OntologyEntry,
    db: Database,
    style_overlay: str | None,
    *,
    max_referenced: int = 10,
    available_contexts: list[dict] | None = None,
) -> str:
    """Prompt the adapter to produce a new definition consistent with current upstream."""
    entries_repo = OntologyEntryRepository(db)

    parts: list[str] = []
    parts.append("You are an ontology definition editor.")
    parts.append("")
    parts.append(
        "The entry below was previously accepted into the ontology, but "
        "recent changes to its referenced entries have rendered its "
        "definition out-of-date. Produce a single new definition that "
        "is consistent with the CURRENT state of the upstream entries."
    )
    parts.append("")
    parts.append("STALE ENTRY")
    parts.append(f"  Label: {entry.mpl_label}")
    parts.append(f"  Term:  {entry.canonical_term!r}")
    parts.append(f"  Current parent: {entry.parent_label or '(top-level)'}")
    if entry.definitions:
        parts.append("  Current definitions (most recent last):")
        for i, d in enumerate(entry.definitions, 1):
            parts.append(
                f"    {i}. (from {d.model_used or '?'}) {d.text}"
            )
    parts.append("")

    parts.append("REFERENCED UPSTREAMS (current state)")
    for ref_label in entry.references_labels[:max_referenced]:
        ref = entries_repo.get(ref_label)
        if ref is None:
            parts.append(f"  {ref_label}: (no longer exists)")
            continue
        ref_parent = ref.parent_label or "(top-level)"
        parts.append(
            f"  {ref_label} {ref.canonical_term!r}  parent: {ref_parent}"
        )
        if ref.definitions:
            latest = ref.definitions[-1]
            parts.append(
                f"    Current def: {latest.text}"
            )
    parts.append("")

    audit_reasons = [
        r for r in entry.stale_reasons
        if r.get("change_type", "").startswith("audit_")
    ]
    if audit_reasons:
        parts.append("CONSISTENCY ISSUES IDENTIFIED BY AUDIT")
        for r in audit_reasons[-3:]:
            note = r.get("note", "")
            parts.append(f"  - {note}")
        parts.append("")

    if style_overlay:
        block = render_style_block(style_overlay)
        if block:
            parts.append(block)
            parts.append("")

    if available_contexts:
        parts.append("DEFINITION CONTEXTS available in this corpus")
        for c in available_contexts:
            parts.append(f"  - {c.get('name')}: {c.get('description', '')}")
        names = ", ".join(c.get("name", "?") for c in available_contexts)
        parts.append(
            f"  REQUIRED: context_name must be EXACTLY ONE OF: {names}. "
            f"Do NOT use null. Pick the single frame that your "
            f"new_definition speaks within."
        )
        parts.append("")

    parts.append("YOUR TASK")
    parts.append(
        "Produce a single new definition (one or two sentences) for the "
        "entry that accurately captures its meaning given the CURRENT "
        "upstream state. Do NOT propose a new label, parent, or canonical "
        "term — only the definition text."
    )
    parts.append("")
    if available_contexts:
        names = " | ".join(c.get("name", "?") for c in available_contexts)
        parts.append(
            'Output ONLY a JSON object: '
            '{"new_definition": "<text>", '
            '"confidence": <number 0.0-10.0>, '
            '"rationale": "<one sentence on what you changed and why>", '
            f'"context_name": "<one of: {names}>"}}'
        )
    else:
        parts.append(
            'Output ONLY a JSON object: '
            '{"new_definition": "<text>", '
            '"confidence": <number 0.0-10.0>, '
            '"rationale": "<one sentence on what you changed and why>"}'
        )

    return "\n".join(parts)


def parse_redefine_verdict(response_text: str) -> RedefineVerdict:
    text = response_text.strip()
    if not text:
        raise RedefineError("redefine adapter returned empty response")

    if text.startswith("```"):
        parts = text.split("```", 2)
        if len(parts) >= 2:
            inner = parts[1]
            if inner.startswith("json"):
                inner = inner[4:].lstrip()
            text = inner

    try:
        obj: Any = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < 0 or end <= start:
            raise RedefineError(
                f"no JSON object found in redefine response: {text[:200]!r}"
            )
        try:
            obj = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise RedefineError(f"JSON parse failed: {exc}") from exc

    if not isinstance(obj, dict):
        raise RedefineError(
            f"redefine response is not a JSON object: {type(obj).__name__}"
        )

    new_def = str(obj.get("new_definition", "")).strip()
    if not new_def:
        raise RedefineError("redefine response missing new_definition")

    try:
        confidence = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence != confidence:
        confidence = 0.0
    confidence = max(0.0, min(10.0, confidence))

    rationale = str(obj.get("rationale", "")).strip() or "(no rationale)"

    raw_ctx = obj.get("context_name")
    context_name: str | None = None
    if isinstance(raw_ctx, str):
        cleaned = raw_ctx.strip().strip("'\"")
        if cleaned and cleaned.lower() not in {"null", "none", "unspecified", ""}:
            context_name = cleaned

    return RedefineVerdict(
        new_definition=new_def, confidence=confidence, rationale=rationale,
        context_name=context_name,
    )


def redefine_stale_entry(
    entry: OntologyEntry,
    db: Database,
    adapter: Adapter,
    *,
    style_overlay: str | None = None,
    min_confidence: float = 6.0,
    available_contexts: list[dict] | None = None,
) -> RedefineVerdict | None:
    """Ask `adapter` for a refreshed definition; append + clear stale on success.

    Returns the verdict on success, None when confidence is below
    `min_confidence` (the new definition is not written).
    """
    prompt = build_redefine_prompt(
        entry, db, style_overlay, available_contexts=available_contexts,
    )
    response = adapter.generate(prompt, want_json=True)
    verdict = parse_redefine_verdict(response.text)

    if verdict.confidence < min_confidence:
        return None

    # Resolve context name → context_id (None if unmatched).
    # Small models (gemma4:e2b) often return null despite being asked to
    # pick; fall back to a keyword-overlap heuristic over the available
    # contexts' descriptions.
    chosen_name = verdict.context_name
    if not chosen_name and available_contexts:
        chosen_name = _infer_context_name(
            verdict.new_definition, available_contexts
        )

    context_id: str | None = None
    if chosen_name:
        from mahalath.db.repositories import DefinitionContextRepository
        ctx = DefinitionContextRepository(db).get_by_name(chosen_name)
        if ctx is not None:
            context_id = ctx.context_id

    now = _utcnow()
    db.ontology_entries.update_one(
        {"_id": entry.mpl_label},
        {
            "$push": {
                "definitions": {
                    "text": verdict.new_definition,
                    "language": "en",
                    "model_used": "rem_redefine",
                    "decision_log_id": None,
                    "context_id": context_id,
                    "created_at": now,
                }
            },
            "$set": {"updated_at": now},
        },
    )
    update_references(db, entry.mpl_label)
    clear_stale(db, entry.mpl_label)
    return verdict


def redefine_pending_stale(
    config: AppConfig,
    db: Database,
    adapter: Adapter,
    *,
    max_items: int = 10,
    min_confidence: float = 6.0,
) -> RedefineResult:
    """Walk audit-flagged stale entries and rewrite definitions in place."""
    stale = stale_entries_with_inconsistent_audit(db, limit=max_items)
    style_overlay = load_style_overlay(config)

    # Snapshot the contexts table once so each redefine call sees them
    # without re-querying.
    from mahalath.db.repositories import DefinitionContextRepository
    available_contexts = [
        {"name": c.name, "description": c.description}
        for c in DefinitionContextRepository(db).all()
    ]

    result = RedefineResult(items_at_start=len(stale))

    for entry in stale:
        try:
            verdict = redefine_stale_entry(
                entry, db, adapter,
                style_overlay=style_overlay,
                min_confidence=min_confidence,
                available_contexts=available_contexts or None,
            )
        except (AdapterError, RedefineError) as exc:
            result.items_errored += 1
            result.errors.append(f"{entry.mpl_label}: {exc}")
            continue

        if verdict is None:
            result.items_skipped += 1
            log.info(
                "redefine: skipped %s (below min confidence)",
                entry.mpl_label,
            )
            continue

        result.items_redefined += 1
        result.verdicts.append({
            "mpl_label": entry.mpl_label,
            "canonical_term": entry.canonical_term,
            "confidence": verdict.confidence,
            "rationale": verdict.rationale,
            "new_definition": verdict.new_definition,
        })
        log.info(
            "redefine: %s rewritten and cleared (conf %.1f)",
            entry.mpl_label, verdict.confidence,
        )

    return result


def audit_pending_stale(
    config: AppConfig,
    db: Database,
    adapter: Adapter,
    *,
    max_items: int = 10,
    clear_threshold: float = _AUDIT_CLEAR_THRESHOLD,
) -> StalenessAuditResult:
    """Walk list_stale, audit each item, clear if still consistent.

    `clear_threshold` is the minimum confidence required to clear a
    stale flag. Below the threshold, the audit's verdict is appended
    to the entry's stale_reasons but the flag remains.
    """
    stale = list_stale(db, limit=max_items)
    style_overlay = load_style_overlay(config)
    result = StalenessAuditResult(items_at_start=len(stale))

    for entry in stale:
        try:
            verdict = audit_stale_entry(
                entry, db, adapter, style_overlay=style_overlay
            )
        except (AdapterError, AuditError) as exc:
            result.items_errored += 1
            result.errors.append(f"{entry.mpl_label}: {exc}")
            continue

        result.items_audited += 1
        result.verdicts.append({
            "mpl_label": entry.mpl_label,
            "canonical_term": entry.canonical_term,
            "decision": verdict.decision,
            "confidence": verdict.confidence,
            "reasoning": verdict.reasoning,
        })

        if (
            verdict.decision == "consistent"
            and verdict.confidence >= clear_threshold
        ):
            clear_stale(db, entry.mpl_label)
            result.items_cleared += 1
            log.info(
                "audit: cleared %s (%s, conf %.1f)",
                entry.mpl_label, entry.canonical_term, verdict.confidence,
            )
        else:
            db.ontology_entries.update_one(
                {"_id": entry.mpl_label},
                {"$push": {"stale_reasons": {
                    "upstream_label": None,
                    "change_type": f"audit_{verdict.decision}",
                    "changed_at": _utcnow(),
                    "note": (
                        f"staleness audit ({verdict.confidence:.1f}): "
                        f"{verdict.reasoning}"
                    ),
                }}},
            )
            result.items_still_stale += 1
            log.info(
                "audit: kept stale %s (%s, %s/%s conf %.1f)",
                entry.mpl_label, entry.canonical_term,
                verdict.decision, verdict.confidence,
                verdict.confidence,
            )

    return result
