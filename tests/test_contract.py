import dataclasses

from mahalath.contract import (
    CANONICAL_MATCH,
    MATCH_FIELDS,
    annotation_dict,
    validate_match,
)
from mahalath.retrieval import Match


def test_real_match_dataclass_satisfies_the_seam_contract():
    # The provider guarantee: Mahalath's actual Match carries every field consumers
    # depend on. If someone renames e.g. `frames`, this test (not just Tirzah's) breaks.
    real_fields = {f.name for f in dataclasses.fields(Match)}
    assert set(MATCH_FIELDS) <= real_fields, f"Match drifted from the seam contract: {set(MATCH_FIELDS) - real_fields}"

    match = Match(mpl_label="MPL:x", canonical_term="x", score=10, match_kind="exact", frames=["f"], is_stale=False)
    assert validate_match(match) == []
    assert annotation_dict(match) == {
        "mpl_label": "MPL:x",
        "canonical_term": "x",
        "frames": ["f"],
        "match_kind": "exact",
        "is_stale": False,
    }


def test_canonical_fixture_conforms():
    assert validate_match(CANONICAL_MATCH) == []


def test_validation_catches_drift():
    errors = validate_match({"mpl_label": "m", "frames": "not-a-list", "match_kind": "wat"})
    assert any("missing field: canonical_term" in e for e in errors)
    assert any("frames must be a list" in e for e in errors)
    assert any("invalid match_kind: 'wat'" in e for e in errors)
