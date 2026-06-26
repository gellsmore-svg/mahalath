"""Public seam contract for Mahalath retrieval matches (Tirzah <-> Mahalath).

The fields a consumer (e.g. Tirzah's semantic annotation in ``tirzah.semantic``)
relies on from a :class:`mahalath.retrieval.Match`. Encoded here so Mahalath's own
tests fail if the public match shape drifts away from what consumers depend on —
the seam is guaranteed at the source, not just asserted by the consumer.

Pure-stdlib + duck-typed (works on a ``Match`` object or a plain dict).
"""

from __future__ import annotations

from typing import Any

# Match attributes the public retrieval seam guarantees to consumers.
MATCH_FIELDS: tuple[str, ...] = ("mpl_label", "canonical_term", "frames", "match_kind", "is_stale")
# match_kind values; label/exact/alias are confident, partial/text are fuzzy.
MATCH_KINDS: frozenset[str] = frozenset({"label", "exact", "alias", "partial", "text"})

_MISSING = object()


def _get(match: Any, key: str) -> Any:
    if isinstance(match, dict):
        return match.get(key, _MISSING)
    return getattr(match, key, _MISSING)


def validate_match(match: Any) -> list[str]:
    """Conformance errors for a retrieval match (empty list = conformant)."""
    errors = [f"match missing field: {f}" for f in MATCH_FIELDS if _get(match, f) is _MISSING]
    frames = _get(match, "frames")
    if frames is not _MISSING and not isinstance(frames, list):
        errors.append("frames must be a list")
    kind = _get(match, "match_kind")
    if kind is not _MISSING and kind not in MATCH_KINDS:
        errors.append(f"invalid match_kind: {kind!r} (allowed: {sorted(MATCH_KINDS)})")
    return errors


def annotation_dict(match: Any) -> dict[str, Any]:
    """The agreed seam projection of a match (the subset consumers annotate with)."""
    return {field: _get(match, field) for field in MATCH_FIELDS}


# Executable fixture: a known-conformant match in dict form.
CANONICAL_MATCH: dict[str, Any] = {
    "mpl_label": "MPL:dog",
    "canonical_term": "dog",
    "frames": ["animal", "pet"],
    "match_kind": "exact",
    "is_stale": False,
}
