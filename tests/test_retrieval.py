"""Retrieval layer (S-A): shared scorer, search_terms, get_codified.

Runs against a live MongoDB test database; the `mongo_db` fixture calls
ensure_indexes, so the `$text` index used by fuzzy search is present.
"""

from __future__ import annotations

from mahalath.db.models import (
    DefinitionContext,
    DefinitionVersion,
    OntologyEntry,
)
from mahalath.db.repositories import (
    DefinitionContextRepository,
    OntologyEntryRepository,
)
from mahalath.retrieval import (
    Filters,
    get_codified,
    score_entry,
    search_terms,
)


def _seed(mongo_db):
    ctx_repo = DefinitionContextRepository(mongo_db)
    ctx_st = DefinitionContext(name="structural", description="Structural frame.")
    ctx_th = DefinitionContext(name="theological", description="Theological frame.")
    ctx_repo.insert(ctx_st)
    ctx_repo.insert(ctx_th)

    repo = OntologyEntryRepository(mongo_db)
    repo.insert(OntologyEntry(
        mpl_label="MPL-001", canonical_term="alpha", confidence=9.0,
        definitions=[DefinitionVersion(
            text="The foundational vorton structure of everything.",
            model_used="gemma4:e2b", context_id=ctx_st.context_id)],
    ))
    repo.insert(OntologyEntry(
        mpl_label="MPL-002", canonical_term="beta", confidence=8.0,
        parent_label="MPL-001",
        aliases=["betaform"],
        references_labels=["MPL-001"],
        definitions=[
            DefinitionVersion(text="Beta structural meaning.",
                              model_used="gemma4:e2b", context_id=ctx_st.context_id),
            DefinitionVersion(text="Beta theological meaning.",
                              model_used="operator", context_id=ctx_th.context_id),
        ],
    ))
    repo.insert(OntologyEntry(
        mpl_label="MPL-003", canonical_term="gamma", confidence=5.0,
        status="draft",
        definitions=[DefinitionVersion(text="Gamma meaning.", model_used="operator")],
    ))
    return ctx_st, ctx_th


# --- score_entry ----------------------------------------------------------


def test_score_entry_canonical_term() -> None:
    entry = OntologyEntry(mpl_label="MPL-001", canonical_term="substrate", confidence=8.0)
    assert score_entry(entry, "what is the substrate here") == 20


def test_score_entry_label_and_alias() -> None:
    entry = OntologyEntry(
        mpl_label="MPL-001", canonical_term="x", confidence=8.0,
        aliases=["medium"],
    )
    assert score_entry(entry, "see mpl-001") == 30
    assert score_entry(entry, "the medium of things") == 15


def test_score_entry_short_term_not_matched() -> None:
    # < 4 chars must not match (avoids over-matching common words).
    entry = OntologyEntry(mpl_label="MPL-001", canonical_term="ion", confidence=8.0)
    assert score_entry(entry, "an ion moves") == 0


# --- search_terms ---------------------------------------------------------


def test_search_exact_ranks_first(mongo_db) -> None:
    _seed(mongo_db)
    matches = search_terms(mongo_db, ["beta"])
    assert matches[0].mpl_label == "MPL-002"
    assert matches[0].match_kind == "exact"
    assert set(matches[0].frames) == {"structural", "theological"}


def test_search_text_finds_definition_body(mongo_db) -> None:
    _seed(mongo_db)
    # "vorton" appears only in MPL-001's definition body — caught via $text.
    matches = search_terms(mongo_db, ["vorton"])
    labels = [m.mpl_label for m in matches]
    assert "MPL-001" in labels
    assert next(m for m in matches if m.mpl_label == "MPL-001").match_kind == "text"


def test_search_filter_status(mongo_db) -> None:
    _seed(mongo_db)
    matches = search_terms(mongo_db, ["gamma"], filters=Filters(status="accepted"))
    assert all(m.mpl_label != "MPL-003" for m in matches)  # MPL-003 is draft


def test_search_filter_min_confidence(mongo_db) -> None:
    _seed(mongo_db)
    matches = search_terms(mongo_db, ["gamma"], filters=Filters(min_confidence=8.0))
    assert all(m.mpl_label != "MPL-003" for m in matches)  # confidence 5.0


def test_search_filter_context(mongo_db) -> None:
    _seed(mongo_db)
    matches = search_terms(mongo_db, ["beta", "alpha"],
                           filters=Filters(context_name="theological"))
    # Only MPL-002 carries a theological definition.
    assert [m.mpl_label for m in matches] == ["MPL-002"]


def test_search_filter_branch(mongo_db) -> None:
    _seed(mongo_db)
    matches = search_terms(mongo_db, ["alpha", "beta"], filters=Filters(branch="MPL-001"))
    labels = {m.mpl_label for m in matches}
    assert "MPL-002" in labels        # under MPL-001
    assert "MPL-003" not in labels     # unrelated branch


# --- get_codified ---------------------------------------------------------


def test_get_codified_full(mongo_db) -> None:
    _seed(mongo_db)
    ref = get_codified(mongo_db, "MPL-002")
    assert ref is not None
    assert ref.canonical_term == "beta"
    assert ref.path == ["MPL-001"]
    assert ref.parent_label == "MPL-001"
    assert len(ref.meanings) == 2  # all frames returned (ADR-022)
    assert {m.context_name for m in ref.meanings} == {"structural", "theological"}
    assert ref.references == ["MPL-001"]


def test_get_codified_reverse_references(mongo_db) -> None:
    _seed(mongo_db)
    ref = get_codified(mongo_db, "MPL-001")
    # MPL-002 references MPL-001, so it appears in referenced_by.
    assert "MPL-002" in ref.referenced_by


def test_get_codified_frame_scoped(mongo_db) -> None:
    _seed(mongo_db)
    ref = get_codified(mongo_db, "MPL-002#theological")
    assert ref is not None
    assert len(ref.meanings) == 1
    assert ref.meanings[0].context_name == "theological"


def test_get_codified_unknown_label_returns_none(mongo_db) -> None:
    _seed(mongo_db)
    assert get_codified(mongo_db, "MPL-999") is None
    assert get_codified(mongo_db, "not-a-label") is None


def test_get_codified_unknown_frame_returns_none(mongo_db) -> None:
    _seed(mongo_db)
    assert get_codified(mongo_db, "MPL-002#nonexistent") is None
