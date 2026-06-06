"""MPL label format tests.

Covers parse/format roundtrip, validation of malformed strings, and the
next_* successor helpers used during ontology entry creation.
"""

from __future__ import annotations

import pytest

from mahalath.labels import (
    MplLabel,
    is_valid,
    next_child,
    next_top_level,
    next_variant,
    parse,
)


def test_parse_roundtrip_top_level() -> None:
    label = parse("MPL-001")
    assert label.segments == (1,)
    assert label.suffix is None
    assert str(label) == "MPL-001"
    assert label.depth == 1


def test_parse_roundtrip_mid_level() -> None:
    label = parse("MPL-001.002")
    assert label.segments == (1, 2)
    assert str(label) == "MPL-001.002"
    assert label.depth == 2


def test_parse_roundtrip_leaf_with_variant() -> None:
    label = parse("MPL-001.002.003a")
    assert label.segments == (1, 2, 3)
    assert label.suffix == "a"
    assert str(label) == "MPL-001.002.003a"
    assert label.depth == 3


def test_parse_accepts_elided_mid_level_example() -> None:
    # The example label from requirements: 000 is valid as a segment value.
    label = parse("MPL-001.000.001a")
    assert label.segments == (1, 0, 1)
    assert label.suffix == "a"
    assert str(label) == "MPL-001.000.001a"


def test_parse_rejects_unpadded_segments() -> None:
    with pytest.raises(ValueError):
        parse("MPL-1.2.3")


def test_parse_rejects_uppercase_suffix() -> None:
    with pytest.raises(ValueError):
        parse("MPL-001.002.003A")


def test_parse_rejects_missing_prefix() -> None:
    with pytest.raises(ValueError):
        parse("001.002.003")


def test_parse_rejects_multi_letter_suffix() -> None:
    with pytest.raises(ValueError):
        parse("MPL-001ab")


def test_is_valid_smoke() -> None:
    assert is_valid("MPL-001")
    assert is_valid("MPL-001.002")
    assert is_valid("MPL-001.002.003")
    assert is_valid("MPL-001.002.003a")
    assert not is_valid("MPL-1.2.3")
    assert not is_valid("nonsense")


def test_parent_top_level_has_none() -> None:
    assert parse("MPL-001").parent() is None


def test_parent_drops_last_segment() -> None:
    assert str(parse("MPL-001.002.003").parent()) == "MPL-001.002"


def test_parent_of_variant_drops_suffix_only() -> None:
    # A variant's "parent" is the base label, not the grandparent.
    assert str(parse("MPL-001.002.003a").parent()) == "MPL-001.002.003"


def test_next_top_level_first_unused() -> None:
    assert next_top_level([]) == "MPL-001"
    assert next_top_level(["MPL-001"]) == "MPL-002"
    assert next_top_level(["MPL-001", "MPL-003"]) == "MPL-002"


def test_next_top_level_ignores_deeper_labels() -> None:
    # A child of MPL-001 should not steal a top-level slot.
    assert next_top_level(["MPL-001", "MPL-001.002"]) == "MPL-002"


def test_next_top_level_ignores_variants() -> None:
    # MPL-001a is not a top-level slot — it's a variant of MPL-001.
    assert next_top_level(["MPL-001", "MPL-001a"]) == "MPL-002"


def test_next_child_under_parent() -> None:
    assert next_child("MPL-001", []) == "MPL-001.001"
    assert next_child("MPL-001", ["MPL-001.001"]) == "MPL-001.002"
    assert (
        next_child("MPL-001", ["MPL-001.001", "MPL-001.003"])
        == "MPL-001.002"
    )


def test_next_child_only_counts_direct_descendants() -> None:
    # Children of MPL-002 should not affect numbering under MPL-001.
    assert (
        next_child("MPL-001", ["MPL-002.001", "MPL-002.002"])
        == "MPL-001.001"
    )


def test_next_child_rejects_variant_as_parent() -> None:
    with pytest.raises(ValueError):
        next_child("MPL-001a", [])


def test_next_variant_first_unused() -> None:
    assert next_variant("MPL-001.002.003", []) == "MPL-001.002.003a"
    assert (
        next_variant("MPL-001.002.003", ["MPL-001.002.003a"])
        == "MPL-001.002.003b"
    )


def test_next_variant_rejects_variant_base() -> None:
    with pytest.raises(ValueError):
        next_variant("MPL-001a", [])


def test_mpl_label_constructor_validates_range() -> None:
    with pytest.raises(ValueError):
        MplLabel((1000,))
    with pytest.raises(ValueError):
        MplLabel(())
    with pytest.raises(ValueError):
        MplLabel((-1,))


def test_mpl_label_constructor_validates_suffix() -> None:
    with pytest.raises(ValueError):
        MplLabel((1,), suffix="A")
    with pytest.raises(ValueError):
        MplLabel((1,), suffix="ab")
