"""Materialised ancestor paths for ontology entries (S-B of the
retrieval spec, docs/retrieval-spec.md).

`OntologyEntry.path` is the denormalised root-first list of ancestor
MPL labels (excluding the entry itself). It is derivable from the
`parent_label` chain at any time, so this module is purely a
maintenance/optimisation layer: it keeps `path` in sync whenever a
parent changes and backfills it for legacy databases.

Maintenance points (every place `parent_label` changes):

  - insert            → `ontology._write_accepted` sets `path` on the
    entry before insert (computed from the parent's own path).
  - re-parent         → `actions._apply_parent` calls `propagate_paths`
    (the moved node AND every descendant shift ancestor chains).
  - rollback/restore  → `proposals._rollback_parent` calls
    `propagate_paths` after restoring/clearing the parent.

Reads should go through `resolved_path`, which prefers the stored path
but falls back to a live walk for un-backfilled legacy entries (a node
with a `parent_label` but an empty stored `path`), so retrieval is
correct on databases that predate this slice.
"""

from __future__ import annotations

from pymongo.database import Database

from mahalath.db.models import OntologyEntry
from mahalath.db.repositories import OntologyEntryRepository

_MAX_DEPTH = 50


def compute_path(
    db: Database, label: str, *, max_depth: int = _MAX_DEPTH
) -> list[str]:
    """Walk `parent_label` to the root; return ancestors root-first.

    The canonical derivation of an entry's path. Cycle-safe via a
    visited set and bounded by `max_depth`. Excludes `label` itself.
    Returns `[]` for a root entry or an unknown label.
    """
    repo = OntologyEntryRepository(db)
    path: list[str] = []
    seen: set[str] = {label}
    cur = repo.get(label)
    depth = 0
    while (
        cur is not None
        and cur.parent_label
        and cur.parent_label not in seen
        and depth < max_depth
    ):
        seen.add(cur.parent_label)
        path.append(cur.parent_label)
        cur = repo.get(cur.parent_label)
        depth += 1
    path.reverse()
    return path


def resolved_path(db: Database, entry: OntologyEntry) -> list[str]:
    """The entry's path, preferring the stored field.

    Falls back to a live `compute_path` walk when the stored path is
    empty but the entry has a parent — i.e. a legacy entry that predates
    the `path` field and has not been backfilled yet. A genuine root
    (no parent) correctly resolves to `[]` without a walk.
    """
    if entry.path:
        return list(entry.path)
    if entry.parent_label:
        return compute_path(db, entry.mpl_label)
    return []


def set_path(db: Database, label: str) -> list[str]:
    """Recompute and persist one entry's path; return it."""
    path = compute_path(db, label)
    db.ontology_entries.update_one({"_id": label}, {"$set": {"path": path}})
    return path


def propagate_paths(db: Database, root_label: str) -> int:
    """Recompute `path` for `root_label` and all of its descendants.

    Called after a re-parent: the moved node's ancestor chain changed,
    and every entry beneath it inherits that change. Walks descendants
    via the denormalised `parent_label` (the authoritative tree edge is
    mirrored there). Cycle-safe via a visited set. Returns the number of
    entries whose path was rewritten.
    """
    repo = OntologyEntryRepository(db)
    updated = 0
    stack = [root_label]
    seen: set[str] = set()
    while stack:
        label = stack.pop()
        if label in seen:
            continue
        seen.add(label)
        set_path(db, label)
        updated += 1
        stack.extend(repo.labels_under_parent(label))
    return updated


def backfill_paths(db: Database) -> dict[str, int]:
    """Recompute `path` for every entry. One-shot legacy migration.

    Mirrors `staleness.backfill_references`: idempotent, writes only
    when the path actually changes. Returns {scanned, updated}.
    """
    scanned = 0
    updated = 0
    repo = OntologyEntryRepository(db)
    for label in repo.all_labels():
        entry = repo.get(label)
        if entry is None:
            continue
        scanned += 1
        new_path = compute_path(db, label)
        if new_path != list(entry.path or []):
            db.ontology_entries.update_one(
                {"_id": label}, {"$set": {"path": new_path}}
            )
            updated += 1
    return {"scanned": scanned, "updated": updated}
