"""Mahalath's Keturah manifest — its LLM-consumable interfaces.

Built from mahalath.contract so the advertised retrieval shape matches the Match
seam consumers depend on.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _pkg_version

from keturah import Manifest, capability, manifest

from mahalath.contract import MATCH_FIELDS, MATCH_KINDS


def _version() -> str:
    try:
        return _pkg_version("mahalath")
    except PackageNotFoundError:
        return "0.0.0+source"


def build_manifest() -> Manifest:
    return manifest(
        "mahalath",
        version=_version(),
        description="Semantic ontology / meaning-precision substrate: resolve terms to MPL labels and senses.",
        capabilities=[
            capability(
                "retrieve",
                "Resolve terms to Mahalath MPL labels for precise meaning. Returns matches carrying "
                + ", ".join(MATCH_FIELDS)
                + " (match_kind one of "
                + ", ".join(sorted(MATCH_KINDS))
                + ").",
                input_schema={
                    "type": "object",
                    "properties": {
                        "terms": {"type": "array", "items": {"type": "string"}},
                        "language": {"type": "string"},
                    },
                    "required": ["terms"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "matches": {
                            "type": "array",
                            "items": {"type": "object", "properties": {field: {} for field in MATCH_FIELDS}},
                        }
                    },
                },
                tags=["semantic", "retrieval", "mpl"],
            ),
        ],
    )
