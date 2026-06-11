"""Intent taxonomy: governed illocution tags (I-A of the intent extension).

Per ADR-024/025 (and the operator's locution/illocution proposal,
docs/intent-extension-discussion.md), a definition may carry *intent
tags* — why the corpus deploys the concept — alongside its meaning
frame. The tags themselves are governed taxonomy rows: `DefinitionContext`
documents with `kind="intent"`, operator-authored and operator-edited,
exactly like the meaning frames they sit beside.

Hard rules (ADR-024):

  - Intent annotates definitions. It never creates entries, never
    partitions an entry, never appears in an MPL label.
  - Intent is deployment metadata ABOUT THE SOURCE ("the corpus deploys
    this concept to teach"), never the term's semantics — the §4
    neutrality NFR stays intact.

This module owns the taxonomy mechanics: a starter vocabulary distilled
from the operator's discussion doc, an idempotent seeder, and the
resolution helper I-B's ingestion path will use. Model-sourced tagging
(debate contract, multi-pass unanimity, the I-D go/no-go gate) is I-B,
not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pymongo.database import Database

from mahalath.db.models import DefinitionContext
from mahalath.db.repositories import DefinitionContextRepository


# Starter vocabulary, distilled from the illocution examples in
# docs/intent-extension-discussion.md. Descriptions are deliberately
# worded as source-deployment metadata (what the SOURCE is doing), not
# as properties of the concept. The operator owns this list: edit
# descriptions, remove tags, add corpus-specific ones via add-context
# --kind intent.
STANDARD_INTENTS: tuple[tuple[str, str], ...] = (
    ("teach",
     "The source deploys the concept to build understanding — to "
     "transfer a model of how things are."),
    ("persuade",
     "The source deploys the concept to move the reader toward "
     "accepting a position or conclusion."),
    ("reassure",
     "The source deploys the concept to settle doubt or maintain trust "
     "without advancing a new claim."),
    ("challenge",
     "The source deploys the concept to unsettle an assumed position "
     "and prompt re-examination."),
    ("warn",
     "The source deploys the concept to flag a danger or consequence "
     "and steer away from a path."),
    ("restore",
     "The source deploys the concept to repair standing, relationship, "
     "or meaning — a renewal framing."),
    ("prompt_reflection",
     "The source deploys the concept to open a question for the reader "
     "rather than settle one."),
    ("create_obligation",
     "The source deploys the concept to bind the reader to an "
     "expectation, duty, or commitment."),
)


@dataclass
class SeedResult:
    inserted: list[str] = field(default_factory=list)
    skipped_existing: list[str] = field(default_factory=list)
    name_conflicts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "inserted": list(self.inserted),
            "skipped_existing": list(self.skipped_existing),
            "name_conflicts": list(self.name_conflicts),
        }


def seed_intents(
    db: Database,
    *,
    intents: tuple[tuple[str, str], ...] = STANDARD_INTENTS,
    created_by: str = "operator",
    dry_run: bool = False,
) -> SeedResult:
    """Insert the intent taxonomy, skip-if-name-exists (idempotent).

    Same idiom as the 2026-06-10 frame seeding: an existing row with the
    same name is left untouched. Names are one namespace across kinds
    (unique index), so a FRAME already holding a proposed intent name is
    reported as a conflict rather than silently skipped — the operator
    must rename one of them.
    """
    repo = DefinitionContextRepository(db)
    result = SeedResult()
    for name, description in intents:
        existing = repo.get_by_name(name)
        if existing is not None:
            if existing.kind == "intent":
                result.skipped_existing.append(name)
            else:
                result.name_conflicts.append(name)
            continue
        if not dry_run:
            repo.insert(DefinitionContext(
                name=name,
                description=description,
                kind="intent",
                created_by=created_by,
            ))
        result.inserted.append(name)
    return result


def resolve_intent_tag(db: Database, name_or_id: str) -> str | None:
    """Resolve an intent name or context_id to its context_id.

    Returns None for unknown values AND for frame rows — an intent tag
    must reference the intent taxonomy, never a meaning frame
    (ADR-024). This is the validation gate I-B's ingestion path uses
    before writing `intent_tags`.
    """
    repo = DefinitionContextRepository(db)
    ctx = repo.get(name_or_id)
    if ctx is None:
        ctx = repo.get_by_name(name_or_id)
    if ctx is None or ctx.kind != "intent":
        return None
    return ctx.context_id
