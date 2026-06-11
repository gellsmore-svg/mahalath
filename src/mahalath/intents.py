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
from mahalath.db.repositories import (
    DefinitionContextRepository,
    OntologyEntryRepository,
)


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


# --- Model-sourced attribution (I-B) ----------------------------------------
#
# All model-sourced intent annotation goes through `attribute_intent`'s
# N-pass unanimity gate (ADR-025). The debate output contract is NOT
# extended: a single in-debate sample could never satisfy the unanimity
# requirement, so attribution is a uniform post-accept step instead —
# the process-document pipeline runs a scoped `backfill_intents` at its
# tail (the S2.27 pattern), and the standalone `backfill-intents` CLI
# sweeps legacy definitions.

# Marker the prompt leads with, so MockAdapter-style substring keying
# (the debate Speaker-tag idiom) can target intent prompts in tests.
INTENT_ATTRIBUTION_TAG = "[Task: intent_attribution]"


@dataclass
class IntentVerdict:
    """One attribution pass's parsed output."""

    intent_tags: list[str]                  # taxonomy NAMES as returned
    intentionality: str | None              # low | medium | high | None
    confidence: float


@dataclass
class IntentAttribution:
    """Aggregated (unanimity-gated) outcome for one definition."""

    mpl_label: str
    definition_index: int
    definition_text: str
    passes: int
    unanimous_tags: list[str]               # taxonomy names, every pass agreed
    unanimous_tag_ids: list[str]            # resolved context_ids
    intentionality: str | None              # only if every pass agreed
    intent_confidence: float | None         # min across passes
    stored: bool
    outcome: str                            # stored | below_threshold |
                                            # no_unanimous_tags | parse_failed
    per_pass: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "mpl_label": self.mpl_label,
            "definition_index": self.definition_index,
            "passes": self.passes,
            "unanimous_tags": list(self.unanimous_tags),
            "intentionality": self.intentionality,
            "intent_confidence": self.intent_confidence,
            "stored": self.stored,
            "outcome": self.outcome,
            "per_pass": list(self.per_pass),
        }


def build_intent_prompt(
    term: str,
    definition_text: str,
    intents: list[DefinitionContext],
    *,
    style_overlay: str | None = None,
) -> str:
    """Prompt for one attribution pass.

    Neutrality stance (ADR-024): the question is what the SOURCE is
    doing by deploying the concept — deployment metadata — never what
    the concept "really intends".
    """
    from mahalath.style import render_style_block

    lines = [
        INTENT_ATTRIBUTION_TAG,
        "You are annotating a glossary definition with the SOURCE's "
        "communicative intent: why does the corpus deploy this concept?",
        "",
    ]
    style_block = render_style_block(style_overlay)
    if style_block:
        lines.extend([style_block, ""])
    lines.extend([
        f'Term: "{term}"',
        f"Definition: {definition_text}",
        "",
        "Available intent tags (use ONLY these names):",
    ])
    for c in intents:
        lines.append(f"  - {c.name}: {c.description}")
    lines.extend([
        "",
        "Also estimate INTENTIONALITY: how deliberately constructed the "
        "source expression appears (low = casual/incidental, medium = "
        "worked but informal, high = carefully argued/published prose).",
        "",
        "Rules:",
        "  - Tag the SOURCE's deployment, not the concept's own meaning.",
        "  - Propose at most 2 tags; an empty list is a valid answer.",
        "  - Do not invent tag names.",
        "",
        'Output ONLY a JSON object of the form {"intent_tags": '
        '["<name>", ...], "intentionality": "low"|"medium"|"high", '
        '"confidence": <number 0.0-10.0>}. No preamble, no Markdown, '
        "no commentary outside the JSON.",
    ])
    return "\n".join(lines)


def parse_intent_verdict(text: str) -> IntentVerdict | None:
    """Tolerant parse of one pass's response; None when unusable."""
    import json as _json

    cleaned = text.strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```", 2)
        if len(parts) >= 2:
            inner = parts[1]
            if inner.startswith("json"):
                inner = inner[4:].lstrip()
            cleaned = inner
    try:
        obj = _json.loads(cleaned)
    except _json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            obj = _json.loads(cleaned[start:end + 1])
        except _json.JSONDecodeError:
            return None
    if not isinstance(obj, dict):
        return None

    raw_tags = obj.get("intent_tags")
    tags = (
        [str(t).strip() for t in raw_tags if str(t).strip()]
        if isinstance(raw_tags, list) else []
    )
    raw_intentionality = obj.get("intentionality")
    intentionality = (
        raw_intentionality
        if raw_intentionality in ("low", "medium", "high") else None
    )
    try:
        confidence = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(10.0, confidence))
    return IntentVerdict(
        intent_tags=tags,
        intentionality=intentionality,
        confidence=confidence,
    )


def attribute_intent(
    db: Database,
    mpl_label: str,
    definition_index: int,
    adapter,
    *,
    passes: int = 3,
    min_confidence: float = 8.0,
    apply: bool = False,
    style_overlay: str | None = None,
) -> IntentAttribution | None:
    """Run N independent attribution passes; store only on unanimity.

    The ADR-025 gate end-to-end:

      - a tag is kept only if EVERY pass proposes it (set intersection);
      - intentionality is kept only if every pass returns the same
        ordinal;
      - intent_confidence = min across passes (the DQ-003 idiom);
      - below `min_confidence`, or with nothing unanimous, NOTHING is
        written — the attribution is returned for operator review in
        the caller's report instead.

    Tags and the ordinal gate independently: a unanimous in-vocabulary
    intentionality is storable even when every proposed tag was
    off-vocabulary (the tags are dropped; the ordinal lands).

    Tag names are resolved through `resolve_intent_tag`, so a frame
    name or invented name from the model can never land in
    `intent_tags`. Returns None when the entry/definition is missing
    or the intent taxonomy is empty.
    """
    from mahalath.adapters.base import AdapterError  # noqa: F401 (re-raise passthrough)

    repo = OntologyEntryRepository(db)
    entry = repo.get(mpl_label)
    if entry is None or definition_index >= len(entry.definitions):
        return None
    intents = DefinitionContextRepository(db).all(kind="intent")
    if not intents:
        return None
    definition = entry.definitions[definition_index]

    prompt = build_intent_prompt(
        entry.canonical_term, definition.text, intents,
        style_overlay=style_overlay,
    )

    verdicts: list[IntentVerdict | None] = []
    per_pass: list[dict] = []
    for _ in range(max(1, passes)):
        response = adapter.generate(prompt, want_json=True)
        verdict = parse_intent_verdict(response.text)
        verdicts.append(verdict)
        per_pass.append(
            {
                "intent_tags": verdict.intent_tags,
                "intentionality": verdict.intentionality,
                "confidence": verdict.confidence,
            }
            if verdict is not None else {"parse_failed": True}
        )

    parsed = [v for v in verdicts if v is not None]
    result = IntentAttribution(
        mpl_label=mpl_label,
        definition_index=definition_index,
        definition_text=definition.text,
        passes=len(verdicts),
        unanimous_tags=[],
        unanimous_tag_ids=[],
        intentionality=None,
        intent_confidence=None,
        stored=False,
        outcome="parse_failed",
        per_pass=per_pass,
    )
    if len(parsed) != len(verdicts):
        return result  # any unparseable pass voids unanimity

    # Per-tag unanimity: a tag survives only if every pass proposed it.
    # Case-folded comparison; resolution validates against the taxonomy.
    tag_sets = [{t.casefold() for t in v.intent_tags} for v in parsed]
    unanimous_cf = set.intersection(*tag_sets) if tag_sets else set()
    name_by_cf = {c.name.casefold(): c.name for c in intents}
    unanimous_names = sorted(
        name_by_cf[cf] for cf in unanimous_cf if cf in name_by_cf
    )
    unanimous_ids = [
        tag_id
        for name in unanimous_names
        if (tag_id := resolve_intent_tag(db, name)) is not None
    ]

    intentionalities = {v.intentionality for v in parsed}
    unanimous_intentionality = (
        parsed[0].intentionality
        if len(intentionalities) == 1 else None
    )
    min_conf = min(v.confidence for v in parsed)

    result.unanimous_tags = unanimous_names
    result.unanimous_tag_ids = unanimous_ids
    result.intentionality = unanimous_intentionality
    result.intent_confidence = min_conf

    if not unanimous_ids and unanimous_intentionality is None:
        result.outcome = "no_unanimous_tags"
        return result
    if min_conf < min_confidence:
        result.outcome = "below_threshold"
        return result

    result.outcome = "stored"
    if apply:
        from datetime import datetime, timezone
        db.ontology_entries.update_one(
            {"_id": mpl_label},
            {"$set": {
                f"definitions.{definition_index}.intent_tags": unanimous_ids,
                f"definitions.{definition_index}.intentionality":
                    unanimous_intentionality,
                f"definitions.{definition_index}.intent_confidence": min_conf,
                "updated_at": datetime.now(timezone.utc),
            }},
        )
        result.stored = True
    return result


@dataclass
class IntentBackfillResult:
    unattributed_at_start: int = 0
    attempted: int = 0
    stored: int = 0
    below_threshold: int = 0
    no_unanimous: int = 0
    errored: int = 0
    attributions: list[IntentAttribution] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "unattributed_at_start": self.unattributed_at_start,
            "attempted": self.attempted,
            "stored": self.stored,
            "below_threshold": self.below_threshold,
            "no_unanimous": self.no_unanimous,
            "errored": self.errored,
            "attributions": [a.to_dict() for a in self.attributions],
            "errors": list(self.errors),
        }


def backfill_intents(
    db: Database,
    adapter,
    *,
    max_items: int = 50,
    passes: int = 3,
    min_confidence: float = 8.0,
    apply: bool = False,
    only_labels: set[str] | None = None,
    style_overlay: str | None = None,
) -> IntentBackfillResult:
    """Walk unattributed definitions; run the unanimity-gated attribution.

    Mirrors `backfill_definition_contexts`: dry-run by default (the
    result carries every attribution for operator review — including
    the below-threshold and non-unanimous ones, per ADR-025's "route to
    operator review"); `apply=True` writes the stored ones.
    `only_labels` scopes to the entries one pipeline run touched.

    "Unattributed" = no intent_tags AND no intentionality AND no
    intent_confidence. A definition the gate declined to annotate stays
    unattributed and WILL be re-attempted on a later sweep — by design,
    while the model pathway is under I-D evaluation.
    """
    from mahalath.adapters.base import AdapterError

    entry_repo = OntologyEntryRepository(db)
    if not DefinitionContextRepository(db).all(kind="intent"):
        return IntentBackfillResult()  # no taxonomy, nothing to do

    unattributed: list[tuple[str, int]] = []
    for label in sorted(entry_repo.all_labels()):
        if only_labels is not None and label not in only_labels:
            continue
        entry = entry_repo.get(label)
        if entry is None:
            continue
        for i, d in enumerate(entry.definitions):
            if not d.intent_tags and d.intentionality is None \
                    and d.intent_confidence is None:
                unattributed.append((label, i))

    result = IntentBackfillResult(unattributed_at_start=len(unattributed))

    for label, idx in unattributed[:max_items]:
        try:
            attribution = attribute_intent(
                db, label, idx, adapter,
                passes=passes,
                min_confidence=min_confidence,
                apply=apply,
                style_overlay=style_overlay,
            )
        except AdapterError as exc:
            result.errored += 1
            result.errors.append(f"{label}#{idx}: {exc}")
            continue
        if attribution is None:
            continue
        result.attempted += 1
        result.attributions.append(attribution)
        if attribution.outcome == "stored":
            result.stored += 1
        elif attribution.outcome == "below_threshold":
            result.below_threshold += 1
        else:
            result.no_unanimous += 1

    return result
