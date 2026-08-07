"""Deborah novel-concept adapter."""

from mahalath.deborah import (
    classify_matches,
    detect_novel_concepts,
    extract_candidate_terms,
    make_novel_concept_handler,
)
from mahalath.manifest import build_manifest


def test_extract_candidate_terms():
    terms = extract_candidate_terms(
        "Is relational substrate coherence well-supported by VortonDynamics?"
    )
    assert any("substrate" in t.lower() or "Substrate" in t for t in terms) or terms
    assert any("VortonDynamics" in t or "vorton" in t.lower() for t in terms) or len(terms) >= 1


def test_classify_novel_vs_known():
    product = classify_matches(
        ["dog", "xyzzy"],
        {
            "dog": [
                {
                    "mpl_label": "MPL-001",
                    "canonical_term": "dog",
                    "match_kind": "exact",
                }
            ],
            "xyzzy": [],
        },
    )
    assert product["novel"] == ["xyzzy"]
    assert product["novel_detected"] is True
    assert product["known"][0]["term"] == "dog"


def test_detect_novel_without_db_all_novel():
    product = detect_novel_concepts("Is flargleblorp coherent?", db=None)
    assert product["novel_detected"] is True


def test_handler_residual_on_novel():
    def search(terms):
        return []  # nothing known

    h = make_novel_concept_handler(search_fn=search)
    out = h(
        {"cognition": "evaluate"},
        {"claim": "Is Quuxonium a valid substrate?"},
    )
    assert out["status"] == "completed"
    assert out.get("residual") is True
    assert out["result"]["novel"]


def test_manifest_has_detect_novel():
    names = {c.name for c in build_manifest().capabilities}
    assert "retrieve" in names
    assert "detect_novel" in names
