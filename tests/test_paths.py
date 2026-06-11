"""Materialised path maintenance + subtree (S-B of the retrieval spec).

Runs against a live MongoDB test database (the `mongo_db` fixture).
Covers: path set at insert, path re-materialised on re-parent (with
descendant cascade) and on rollback, the `backfill_paths` migration,
`resolved_path`'s legacy fallback, and the `subtree` read view.
"""

from __future__ import annotations

from mahalath.actions import ProposeParent, _apply_parent
from mahalath.db.models import (
    DefinitionVersion,
    OntologyEntry,
    OntologyTreeEdge,
)
from mahalath.db.repositories import (
    OntologyEntryRepository,
    OntologyTreeRepository,
)
from mahalath.paths import (
    backfill_paths,
    compute_path,
    propagate_paths,
    resolved_path,
    set_path,
)
from mahalath.retrieval import subtree


def _entry(label, parent=None, path=None, term=None):
    return OntologyEntry(
        mpl_label=label,
        canonical_term=term or label.lower(),
        confidence=8.0,
        parent_label=parent,
        path=path if path is not None else [],
        definitions=[DefinitionVersion(text=f"{label} meaning.", model_used="op")],
    )


def _chain(mongo_db):
    """MPL-001 → MPL-001.001 → MPL-001.001.001, paths materialised."""
    repo = OntologyEntryRepository(mongo_db)
    tree = OntologyTreeRepository(mongo_db)
    repo.insert(_entry("MPL-001", path=[]))
    repo.insert(_entry("MPL-001.001", parent="MPL-001", path=["MPL-001"]))
    repo.insert(_entry(
        "MPL-001.001.001", parent="MPL-001.001", path=["MPL-001", "MPL-001.001"]
    ))
    tree.add_edge(OntologyTreeEdge(parent_label="MPL-001", child_label="MPL-001.001"))
    tree.add_edge(OntologyTreeEdge(
        parent_label="MPL-001.001", child_label="MPL-001.001.001"
    ))
    return repo


# --- compute_path / resolved_path -----------------------------------------


def test_compute_path_walks_to_root(mongo_db) -> None:
    _chain(mongo_db)
    assert compute_path(mongo_db, "MPL-001.001.001") == ["MPL-001", "MPL-001.001"]
    assert compute_path(mongo_db, "MPL-001") == []


def test_compute_path_cycle_safe(mongo_db) -> None:
    repo = OntologyEntryRepository(mongo_db)
    repo.insert(_entry("MPL-001", parent="MPL-002"))
    repo.insert(_entry("MPL-002", parent="MPL-001"))
    # Must terminate, not loop forever.
    path = compute_path(mongo_db, "MPL-001")
    assert path[-1] == "MPL-002"
    assert "MPL-001" not in path


def test_resolved_path_prefers_stored(mongo_db) -> None:
    repo = OntologyEntryRepository(mongo_db)
    # Stored path deliberately "wrong" — resolved_path trusts the field.
    repo.insert(_entry("MPL-001", parent="MPL-009", path=["MPL-009"]))
    entry = repo.get("MPL-001")
    assert resolved_path(mongo_db, entry) == ["MPL-009"]


def test_resolved_path_fallback_for_legacy(mongo_db) -> None:
    # A child with a parent but an EMPTY stored path (legacy / un-backfilled)
    # falls back to a live walk.
    repo = OntologyEntryRepository(mongo_db)
    repo.insert(_entry("MPL-001", path=[]))
    repo.insert(_entry("MPL-001.001", parent="MPL-001", path=[]))
    entry = repo.get("MPL-001.001")
    assert resolved_path(mongo_db, entry) == ["MPL-001"]


def test_resolved_path_root_is_empty_no_walk(mongo_db) -> None:
    repo = OntologyEntryRepository(mongo_db)
    repo.insert(_entry("MPL-001", path=[]))
    assert resolved_path(mongo_db, repo.get("MPL-001")) == []


# --- set_path / propagate_paths -------------------------------------------


def test_set_path_persists(mongo_db) -> None:
    repo = _chain(mongo_db)
    # Corrupt then repair one entry.
    mongo_db.ontology_entries.update_one(
        {"_id": "MPL-001.001.001"}, {"$set": {"path": []}}
    )
    assert set_path(mongo_db, "MPL-001.001.001") == ["MPL-001", "MPL-001.001"]
    assert repo.get("MPL-001.001.001").path == ["MPL-001", "MPL-001.001"]


def test_propagate_paths_cascades_to_descendants(mongo_db) -> None:
    repo = _chain(mongo_db)
    # Re-home the middle node under a fresh root, then propagate.
    repo.insert(_entry("MPL-002", path=[]))
    mongo_db.ontology_entries.update_one(
        {"_id": "MPL-001.001"}, {"$set": {"parent_label": "MPL-002"}}
    )
    updated = propagate_paths(mongo_db, "MPL-001.001")
    assert updated == 2  # the node + its one descendant
    assert repo.get("MPL-001.001").path == ["MPL-002"]
    assert repo.get("MPL-001.001.001").path == ["MPL-002", "MPL-001.001"]
    # The untouched root is unaffected.
    assert repo.get("MPL-001").path == []


# --- maintenance at the real write points ---------------------------------


def test_reparent_apply_rematerialises_subtree(mongo_db) -> None:
    repo = _chain(mongo_db)
    repo.insert(_entry("MPL-002", path=[]))
    _apply_parent(
        ProposeParent(
            child_label="MPL-001.001", parent_label="MPL-002",
            reason="test", confidence=9.0,
        ),
        mongo_db,
    )
    assert repo.get("MPL-001.001").path == ["MPL-002"]
    assert repo.get("MPL-001.001.001").path == ["MPL-002", "MPL-001.001"]


def test_insert_via_persist_sets_path(mongo_db) -> None:
    # _write_accepted should materialise path from the parent's own path.
    from mahalath.config import RuntimeConfig
    from mahalath.debate import DebateResult
    from mahalath.ontology import persist_debate_result

    repo = OntologyEntryRepository(mongo_db)
    repo.insert(_entry("MPL-001", path=[]))

    result = DebateResult(
        decision_log_id="dl-1",
        term="child-term",
        source_document_id="doc-1",
        outcome="accepted",
        final_definition="A child definition.",
        final_confidence=8.5,
        iterations_used=1,
    )
    out = persist_debate_result(
        result, mongo_db, RuntimeConfig(), parent_label="MPL-001"
    )
    child = repo.get(out.mpl_label)
    assert child.parent_label == "MPL-001"
    assert child.path == ["MPL-001"]


# --- backfill_paths -------------------------------------------------------


def test_backfill_paths_fills_legacy(mongo_db) -> None:
    repo = OntologyEntryRepository(mongo_db)
    repo.insert(_entry("MPL-001", path=[]))
    repo.insert(_entry("MPL-001.001", parent="MPL-001", path=[]))
    repo.insert(_entry(
        "MPL-001.001.001", parent="MPL-001.001", path=[]
    ))
    result = backfill_paths(mongo_db)
    assert result == {"scanned": 3, "updated": 2}  # root already correct
    assert repo.get("MPL-001.001.001").path == ["MPL-001", "MPL-001.001"]
    # Idempotent: a second run rewrites nothing.
    assert backfill_paths(mongo_db)["updated"] == 0


# --- subtree --------------------------------------------------------------


def test_subtree_depth_one(mongo_db) -> None:
    repo = OntologyEntryRepository(mongo_db)
    repo.insert(_entry("MPL-001", path=[]))
    repo.insert(_entry("MPL-001.001", parent="MPL-001", path=["MPL-001"]))
    repo.insert(_entry("MPL-001.002", parent="MPL-001", path=["MPL-001"]))
    repo.insert(_entry(
        "MPL-001.001.001", parent="MPL-001.001", path=["MPL-001", "MPL-001.001"]
    ))
    summary = subtree(mongo_db, "MPL-001", depth=1)
    assert summary.count == 2
    assert {n.mpl_label for n in summary.nodes} == {"MPL-001.001", "MPL-001.002"}
    assert all(n.depth == 1 for n in summary.nodes)


def test_subtree_depth_two(mongo_db) -> None:
    _chain(mongo_db)
    summary = subtree(mongo_db, "MPL-001", depth=2)
    by_label = {n.mpl_label: n for n in summary.nodes}
    assert summary.count == 2
    assert by_label["MPL-001.001"].depth == 1
    assert by_label["MPL-001.001.001"].depth == 2


def test_subtree_unknown_label_is_none(mongo_db) -> None:
    assert subtree(mongo_db, "MPL-999", depth=1) is None


def test_subtree_falls_back_on_unmaterialised_paths(mongo_db) -> None:
    # Legacy DB shape: parents set, paths never backfilled. The path-query
    # fast path would return nothing; subtree must detect this and walk.
    repo = OntologyEntryRepository(mongo_db)
    repo.insert(_entry("MPL-001", path=[]))
    repo.insert(_entry("MPL-001.001", parent="MPL-001", path=[]))
    repo.insert(_entry(
        "MPL-001.001.001", parent="MPL-001.001", path=[]
    ))
    summary = subtree(mongo_db, "MPL-001", depth=2)
    by_label = {n.mpl_label: n for n in summary.nodes}
    assert summary.count == 2
    assert by_label["MPL-001.001"].depth == 1
    assert by_label["MPL-001.001.001"].depth == 2


def test_persist_retro_links_older_entries(mongo_db) -> None:
    # An older entry mentions "vorton" before any vorton entry exists;
    # when the debate lands the new entry, the old entry's references
    # must pick it up without waiting for a manual backfill.
    from mahalath.config import RuntimeConfig
    from mahalath.debate import DebateResult
    from mahalath.ontology import persist_debate_result

    repo = OntologyEntryRepository(mongo_db)
    repo.insert(OntologyEntry(
        mpl_label="MPL-001", canonical_term="weave", confidence=8.0,
        definitions=[DefinitionVersion(
            text="A weave is a braid of vorton threads.", model_used="op"
        )],
    ))

    result = DebateResult(
        decision_log_id="dl-2",
        term="vorton",
        source_document_id="doc-1",
        outcome="accepted",
        final_definition="A stable topological knot.",
        final_confidence=8.5,
        iterations_used=1,
    )
    out = persist_debate_result(result, mongo_db, RuntimeConfig())
    assert repo.get("MPL-001").references_labels == [out.mpl_label]
