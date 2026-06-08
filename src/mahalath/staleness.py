"""Reference tracking + staleness flagging for the ontology graph.

When an entry's definition is written it records which other MPL
labels it explicitly references (regex on `MPL-NNN[.NNN…][a-z]?`).
That gives the database a reverse-index: "who depends on MPL-X?".
When MPL-X is later modified (definition changed, parent re-pointed,
rollback) the dependents are flagged stale. Already-stale entries
receive a new reason appended but stay flagged.

Three things live here:

  Reference extraction.
      `extract_references(text)` regexes label tokens out of a single
      definition text. `compute_references_for_entry(entry)` deduplicates
      across all definitions on an entry, excludes the entry's own label.
      Catch: definitions that reference other entries by canonical
      term ("the Relational Substrate") rather than MPL label go
      undetected here. An LLM extraction pass is the obvious upgrade;
      this slice keeps it regex-only because most agent-generated
      definitions in the current corpus DO carry the MPL identifier.

  Invalidation.
      `mark_dependents_stale(db, upstream_label, ...)` walks the
      reverse-index and flags every entry that references the
      upstream. Cascades by default, so newly-flagged entries
      propagate to THEIR dependents. Cycle-safe via the `visited` set.

  Resolution.
      `clear_stale(db, label)` is the unflag path. REM (next slice) or
      a chat-mediated human review calls this once it's confirmed the
      dependent's content is still valid against the new upstream.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from pymongo.database import Database

from mahalath.db.models import OntologyEntry
from mahalath.db.repositories import OntologyEntryRepository


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
    cascade: bool = True,
    note: str | None = None,
) -> None:
    """Append an operator-authored definition AND propagate staleness.

    This is the canonical way to add an operator definition (replaces
    direct `$push: definitions` patterns). It:
      - appends the definition
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
