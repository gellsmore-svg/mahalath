"""MPL label format utilities.

Per ADR-007 the canonical label format is

    MPL-NNN[.NNN[.NNN...]][a-z]?

where each numeric segment is zero-padded to 3 digits (000-999), the
number of dotted segments is the depth in the ontology tree (top-level
has one segment), and an optional single lowercase letter suffix marks
a variant-of-parent (contradistinct split) entry.

Examples:

    MPL-001                top-level root
    MPL-001.002            second child of MPL-001
    MPL-001.002.003        third child of MPL-001.002
    MPL-001.002.003a       first variant of MPL-001.002.003

This module is the single source of truth for label parsing, formatting,
and successor computation. Callers should not assemble label strings
directly.

Convention: numeric segments start at 001 for newly assigned positions.
000 is reserved for explicit "elided mid level" usage (an entry placed
directly under its grandparent with no intermediate grouping). Agents
may use 000 explicitly during debate but the default next_* helpers
will not produce it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

MPL_PREFIX = "MPL-"
SEGMENT_PAD = 3
MAX_DEPTH = 6  # generous ceiling; real depth grows from debate, not from format
MAX_SEGMENT_VALUE = 999

_PATTERN = re.compile(r"^MPL-(\d{3}(?:\.\d{3})*)([a-z])?$")


@dataclass(frozen=True)
class MplLabel:
    """Structured form of an MPL label."""

    segments: tuple[int, ...]
    suffix: str | None = None

    def __post_init__(self) -> None:
        if not 1 <= len(self.segments) <= MAX_DEPTH:
            raise ValueError(
                f"MplLabel needs 1..{MAX_DEPTH} segments, got {len(self.segments)}"
            )
        for seg in self.segments:
            if not 0 <= seg <= MAX_SEGMENT_VALUE:
                raise ValueError(
                    f"MplLabel segment out of range 0..{MAX_SEGMENT_VALUE}: {seg}"
                )
        if self.suffix is not None and not re.fullmatch(r"[a-z]", self.suffix):
            raise ValueError(f"MplLabel suffix must be single lowercase letter: {self.suffix!r}")

    def __str__(self) -> str:
        body = ".".join(f"{s:0{SEGMENT_PAD}d}" for s in self.segments)
        return f"{MPL_PREFIX}{body}{self.suffix or ''}"

    @property
    def depth(self) -> int:
        return len(self.segments)

    def parent(self) -> "MplLabel | None":
        if self.suffix:
            return MplLabel(self.segments, suffix=None)
        if len(self.segments) <= 1:
            return None
        return MplLabel(self.segments[:-1])


def parse(label: str) -> MplLabel:
    """Parse a string into MplLabel. Raises ValueError on malformed input."""
    m = _PATTERN.match(label)
    if not m:
        raise ValueError(f"Invalid MPL label: {label!r}")
    segs_str, suffix = m.groups()
    segments = tuple(int(s) for s in segs_str.split("."))
    return MplLabel(segments, suffix=suffix)


def is_valid(label: str) -> bool:
    try:
        parse(label)
        return True
    except ValueError:
        return False


def _used_numbers_at_depth(
    existing: Iterable[str],
    target_depth: int,
    parent_segments: tuple[int, ...],
) -> set[int]:
    used: set[int] = set()
    for s in existing:
        try:
            lbl = parse(s)
        except ValueError:
            continue
        if lbl.suffix is not None:
            continue
        if lbl.depth != target_depth:
            continue
        if lbl.segments[: target_depth - 1] != parent_segments:
            continue
        used.add(lbl.segments[-1])
    return used


def next_top_level(existing: Iterable[str]) -> str:
    """Return the next unused top-level MPL label.

    Numbering starts at 001. 000 is reserved (project-root self-reference);
    deeper labels (MPL-001.002, MPL-001.002.003) are ignored when picking
    the next top-level slot.
    """
    used = _used_numbers_at_depth(existing, target_depth=1, parent_segments=())
    for candidate in range(1, MAX_SEGMENT_VALUE + 1):
        if candidate not in used:
            return str(MplLabel((candidate,)))
    raise ValueError("Exhausted top-level MPL numbering range (001-999).")


def next_child(parent: str, existing_children: Iterable[str]) -> str:
    """Return the next unused child label directly under parent.

    Numbering starts at 001. Variant labels (with suffix) cannot be
    parents; pass the base label instead.
    """
    parent_lbl = parse(parent)
    if parent_lbl.suffix is not None:
        raise ValueError(f"Cannot add child under a variant label: {parent}")
    if parent_lbl.depth >= MAX_DEPTH:
        raise ValueError(
            f"Cannot add child under {parent}; max depth is {MAX_DEPTH}."
        )
    used = _used_numbers_at_depth(
        existing_children,
        target_depth=parent_lbl.depth + 1,
        parent_segments=parent_lbl.segments,
    )
    for candidate in range(1, MAX_SEGMENT_VALUE + 1):
        if candidate not in used:
            return str(MplLabel(parent_lbl.segments + (candidate,)))
    raise ValueError(f"Exhausted child numbering under {parent}.")


def next_variant(base: str, existing_variants: Iterable[str]) -> str:
    """Return the next unused single-letter variant suffix on base.

    `base` must itself be a non-variant label. existing_variants may
    contain unrelated labels; they will be filtered.
    """
    base_lbl = parse(base)
    if base_lbl.suffix is not None:
        raise ValueError(f"Cannot add variant of a variant: {base}")
    used: set[str] = set()
    for s in existing_variants:
        try:
            lbl = parse(s)
        except ValueError:
            continue
        if lbl.segments == base_lbl.segments and lbl.suffix is not None:
            used.add(lbl.suffix)
    for ch in "abcdefghijklmnopqrstuvwxyz":
        if ch not in used:
            return str(MplLabel(base_lbl.segments, suffix=ch))
    raise ValueError(f"Exhausted variant suffixes under {base}.")
