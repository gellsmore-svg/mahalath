"""Cross-language mapping machinery (M-C, ADR-029).

A mapping is a typed semantic assertion between two lexicon entries —
never a translation. The constitution mirrors intent attribution:

  - the relationship vocabulary is a governed taxonomy
    (`kind="mapping_relation"` rows, operator-owned);
  - N independent attribution passes (rotated across the
    `consensus_models` roster — cross-FAMILY agreement);
  - the MAJORITY verdict across passes decides (operator-ruled
    2026-06-12, relaxed from strict unanimity after the first live
    dry-run: the families kept agreeing a pair was related while
    splitting partial_overlap vs narrower_than): a majority "none"
    → `rejected`; a majority typed relationship is promoted at the
    standard threshold with confidence = MEDIAN across the typed
    votes (robust to one family's scale skew, the S2.42 pathology);
  - below threshold or no majority → stored as `unresolved`
    (Rule 6: store uncertainty); minority dissents are always kept
    verbatim in `per_pass`;
  - alignment is NEVER performed at ingestion — generation runs as a
    background/REM-style job over already-settled lexicons.

The mapping's `illocution_comparison` confronts how each language
DEPLOYS the term (per-lexicon intent-tag profiles, attributed long
before any mapping existed and therefore uncontaminated by it).
Deployment divergence is translation-risk signal in its own right.

Candidate pairs come from one snapshot-prompt per source entry over
the target lexicon (the hierarchy-review pattern). This is the
pre-embeddings stopgap: O(source-lexicon) prompts, not O(n×m) pairs.
Embeddings (S-E) replace the candidate stage, not the gate.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import median
from typing import Any

from pymongo.database import Database

from mahalath.config import AppConfig
from mahalath.db.models import DefinitionContext, Mapping, OntologyEntry
from mahalath.db.repositories import (
    DefinitionContextRepository,
    MappingRepository,
    OntologyEntryRepository,
)

log = logging.getLogger("mahalath.mappings")

# Prompt markers (the MockAdapter substring-keying idiom).
CANDIDATE_TAG = "[Task: mapping_candidates]"
ATTRIBUTION_TAG = "[Task: mapping_attribution]"

# The model may answer "none" to assert the pair is unrelated; it is
# not a taxonomy row and never stored as a relationship name.
NO_RELATION = "none"

# Starter relationship vocabulary (ADR-029). Operator-owned: edit via
# add-context --kind mapping_relation once seeded.
STANDARD_MAPPING_RELATIONS: tuple[tuple[str, str], ...] = (
    ("equivalent",
     "The two terms' meanings coincide for the corpus's purposes; "
     "either can stand for the other with no loss the corpus would "
     "care about. Symmetric."),
    ("partial_overlap",
     "The meanings share substantial ground but each carries content "
     "the other lacks. Symmetric."),
    ("narrower_than",
     "The SOURCE term's meaning is a proper part of the target's — "
     "everything the source asserts, the target covers, not vice "
     "versa. Directional."),
    ("broader_than",
     "The SOURCE term's meaning properly contains the target's. "
     "Directional; the inverse of narrower_than."),
)


def seed_mapping_relations(
    db: Database, *, dry_run: bool = False, created_by: str = "operator"
) -> dict[str, list[str]]:
    """Idempotent seeder, same idiom as seed_intents (one namespace)."""
    repo = DefinitionContextRepository(db)
    result: dict[str, list[str]] = {
        "inserted": [], "skipped_existing": [], "name_conflicts": [],
    }
    for name, description in STANDARD_MAPPING_RELATIONS:
        existing = repo.get_by_name(name)
        if existing is not None:
            key = ("skipped_existing"
                   if existing.kind == "mapping_relation"
                   else "name_conflicts")
            result[key].append(name)
            continue
        if not dry_run:
            repo.insert(DefinitionContext(
                name=name, description=description,
                kind="mapping_relation", created_by=created_by,
            ))
        result["inserted"].append(name)
    return result


# --- Illocution comparison ---------------------------------------------------


def illocution_profile(db: Database, entry: OntologyEntry) -> list[str]:
    """The entry's deployment profile: intent-tag NAMES across all its
    definitions (deduped, sorted)."""
    id_to_name = {
        c.context_id: c.name
        for c in DefinitionContextRepository(db).all(kind="intent")
    }
    names: set[str] = set()
    for d in entry.definitions:
        for tag_id in d.intent_tags:
            name = id_to_name.get(tag_id)
            if name:
                names.add(name)
    return sorted(names)


def compare_illocution(
    db: Database, source: OntologyEntry, target: OntologyEntry
) -> dict[str, Any]:
    src = illocution_profile(db, source)
    tgt = illocution_profile(db, target)
    shared = sorted(set(src) & set(tgt))
    return {
        "source_intents": src,
        "target_intents": tgt,
        "shared": shared,
        "source_only": sorted(set(src) - set(tgt)),
        "target_only": sorted(set(tgt) - set(src)),
        "divergent": bool((set(src) | set(tgt)) - set(shared)),
    }


# --- Prompts ------------------------------------------------------------------


def _entry_block(entry: OntologyEntry) -> str:
    defs = "\n".join(
        f"  - {d.text}" for d in entry.definitions[-3:]
    ) or "  (no definitions)"
    return (
        f"{entry.mpl_label} ({entry.language}) {entry.canonical_term!r}\n"
        f"{defs}"
    )


def build_candidate_prompt(
    source: OntologyEntry, targets: list[OntologyEntry]
) -> str:
    lines = [
        CANDIDATE_TAG,
        "You are scouting for cross-language semantic relationships "
        "between two lexicons. Below is one SOURCE entry and a snapshot "
        "of the TARGET-language lexicon.",
        "",
        "SOURCE ENTRY",
        _entry_block(source),
        "",
        "TARGET LEXICON",
    ]
    for t in targets:
        latest = t.definitions[-1].text if t.definitions else ""
        lines.append(f"  {t.mpl_label} {t.canonical_term!r}: {latest[:160]}")
    lines += [
        "",
        "List the target entries (at most 3) whose MEANING plausibly "
        "relates to the source entry's meaning. Relatedness of meaning, "
        "not similarity of spelling. An empty list is a good answer.",
        "",
        'Output ONLY a JSON object: {"candidates": ["MPL-xxx", ...]}',
    ]
    return "\n".join(lines)


def build_attribution_prompt(
    source: OntologyEntry,
    target: OntologyEntry,
    relations: list[DefinitionContext],
) -> str:
    rel_lines = [f"  - {r.name}: {r.description}" for r in relations]
    names = " | ".join(r.name for r in relations)
    return "\n".join([
        ATTRIBUTION_TAG,
        "You are asserting the semantic relationship between one term "
        "in each of two language lexicons. This is NOT translation: "
        "judge whether and how the MEANINGS relate, reading each "
        "definition in its own language.",
        "",
        "SOURCE",
        _entry_block(source),
        "",
        "TARGET",
        _entry_block(target),
        "",
        "RELATIONSHIP VOCABULARY (directional types read source-relative)",
        *rel_lines,
        f"  - {NO_RELATION}: the meanings are not usefully related.",
        "",
        "Confidence is on a 0.0-10.0 scale.",
        'Output ONLY a JSON object: {"relationship": '
        f'"<one of: {names} | {NO_RELATION}>", '
        '"confidence": <number>, '
        '"rationale": "<one sentence>"}',
    ])


def parse_mapping_verdict(text: str) -> dict[str, Any] | None:
    """Tolerant parse of one pass; None when unusable."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```", 2)
        if len(parts) >= 2:
            inner = parts[1]
            if inner.startswith("json"):
                inner = inner[4:].lstrip()
            cleaned = inner
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            obj = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict):
        return None
    relationship = str(obj.get("relationship", "")).strip().casefold()
    if not relationship:
        return None
    try:
        confidence = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(10.0, confidence))
    # The intent scale-correction heuristic (S2.42), same rule.
    rescaled_from = None
    if 0 < confidence < 1.0:
        rescaled_from = confidence
        confidence = round(confidence * 10.0, 4)
    return {
        "relationship": relationship,
        "confidence": confidence,
        "rationale": str(obj.get("rationale", "")).strip(),
        **({"confidence_rescaled_from": rescaled_from}
           if rescaled_from is not None else {}),
    }


# --- Attribution gate ---------------------------------------------------------


@dataclass
class MappingAttribution:
    source_label: str
    target_label: str
    relationship: str | None      # unanimous name, or None
    confidence: float | None      # min across passes
    rationale: str                # last pass's rationale for the verdict
    status: str                   # accepted | rejected | unresolved
    per_pass: list[dict] = field(default_factory=list)
    models_used: list[str] = field(default_factory=list)
    stored: bool = False

    def to_dict(self) -> dict:
        return {
            "source_label": self.source_label,
            "target_label": self.target_label,
            "relationship": self.relationship,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "status": self.status,
            "per_pass": list(self.per_pass),
            "models_used": list(self.models_used),
            "stored": self.stored,
        }


def attribute_mapping(
    db: Database,
    source_label: str,
    target_label: str,
    adapter,
    *,
    passes: int = 3,
    min_confidence: float = 8.0,
    models: list[str] | None = None,
    apply: bool = False,
) -> MappingAttribution | None:
    """Run N attribution passes on one pair; gate on majority.

    Outcomes (operator-ruled 2026-06-12): strict-majority typed
    relationship with median-across-typed-votes confidence at/above
    threshold → `accepted`; strict-majority `none` → `rejected`;
    no majority, a parse failure, or under threshold → `unresolved`.
    With `apply=True` the verdict (whatever its status) is upserted
    into `mappings` with full per-pass audit.
    """
    entries = OntologyEntryRepository(db)
    source = entries.get(source_label)
    target = entries.get(target_label)
    if source is None or target is None:
        return None
    relations = DefinitionContextRepository(db).all(kind="mapping_relation")
    if not relations:
        return None
    valid_names = {r.name.casefold(): r for r in relations}

    prompt = build_attribution_prompt(source, target, relations)
    roster = models or []
    per_pass: list[dict] = []
    models_used: list[str] = []
    verdicts: list[dict | None] = []
    for i in range(max(1, passes)):
        model = roster[i % len(roster)] if roster else None
        response = adapter.generate(prompt, want_json=True, model=model)
        models_used.append(response.model)
        verdict = parse_mapping_verdict(response.text)
        verdicts.append(verdict)
        per_pass.append(verdict or {"parse_failed": True})

    result = MappingAttribution(
        source_label=source_label,
        target_label=target_label,
        relationship=None,
        confidence=None,
        rationale="",
        status="unresolved",
        per_pass=per_pass,
        models_used=models_used,
    )
    parsed = [v for v in verdicts if v is not None]
    if len(parsed) == len(verdicts) and parsed:
        result.confidence = min(v["confidence"] for v in parsed)
        result.rationale = parsed[-1]["rationale"]
        tally = Counter(v["relationship"] for v in parsed)
        name, votes = tally.most_common(1)[0]
        if votes * 2 > len(parsed):  # strict majority decides
            majority = [v for v in parsed if v["relationship"] == name]
            result.rationale = majority[-1]["rationale"]
            if name == NO_RELATION:
                result.relationship = NO_RELATION
                result.confidence = median(
                    v["confidence"] for v in majority)
                result.status = "rejected"
            elif name in valid_names:
                # Median over ALL typed votes: a minority typed
                # dissent still asserts relatedness, so its score
                # counts; only "none" votes are excluded.
                typed = [v for v in parsed
                         if v["relationship"] != NO_RELATION]
                conf = median(v["confidence"] for v in typed)
                result.relationship = valid_names[name].name
                result.confidence = conf
                result.status = ("accepted" if conf >= min_confidence
                                 else "unresolved")
            # majority on an unknown name → unresolved (fail closed)
        # no strict majority → unresolved with per-pass detail

    if apply:
        _store_attribution(db, source, target, result, relations)
        result.stored = True
    return result


def _store_attribution(
    db: Database,
    source: OntologyEntry,
    target: OntologyEntry,
    attribution: MappingAttribution,
    relations: list[DefinitionContext],
) -> None:
    rel_id = None
    if attribution.relationship and attribution.relationship != NO_RELATION:
        by_name = {r.name: r.context_id for r in relations}
        rel_id = by_name.get(attribution.relationship)
    now = datetime.now(timezone.utc)
    payload = Mapping(
        source_label=source.mpl_label,
        target_label=target.mpl_label,
        source_language=source.language,
        target_language=target.language,
        relationship=attribution.relationship or "undetermined",
        relationship_id=rel_id,
        confidence=attribution.confidence or 0.0,
        rationale=attribution.rationale,
        illocution_comparison=compare_illocution(db, source, target),
        status=attribution.status,
        per_pass=attribution.per_pass,
        models_used=attribution.models_used,
        updated_at=now,
    ).model_dump()
    db.mappings.update_one(
        {"source_label": source.mpl_label, "target_label": target.mpl_label},
        {"$set": {k: v for k, v in payload.items() if k != "mapping_id"},
         "$setOnInsert": {"mapping_id": payload["mapping_id"]}},
        upsert=True,
    )


# --- Operator adjudication ----------------------------------------------------


class ResolutionError(ValueError):
    """Raised when an operator resolution is malformed or unmappable."""


def resolve_mapping(
    db: Database,
    source_label: str,
    target_label: str,
    *,
    verdict: str,
    rationale: str,
    decided_via: str = "operator",
) -> Mapping:
    """Render a human (or delegate) verdict on a stored mapping.

    `verdict` is a taxonomy relation NAME (→ status `accepted`), or
    `none`/`reject` (→ status `rejected`, relationship `none`). The
    model-consensus audit (`per_pass`, `models_used`) is preserved
    verbatim; `decided_via`/`decision_rationale`/`decided_at` record
    who decided and why, so the operator verdict never masquerades as
    model consensus (and can be excluded from calibration, the S2.43
    `decided_via` intent). Re-audit is the staleness path's job, so a
    resolution also clears any pending `is_stale` flag.
    """
    repo = MappingRepository(db)
    existing = repo.get_pair(source_label, target_label)
    if existing is None:
        raise ResolutionError(
            f"no mapping {source_label} -> {target_label} to resolve")

    v = verdict.strip().casefold()
    relations = DefinitionContextRepository(db).all(kind="mapping_relation")
    by_name = {r.name.casefold(): r for r in relations}
    if v in (NO_RELATION, "reject", "rejected"):
        status, rel_name, rel_id = "rejected", NO_RELATION, None
    elif v in by_name:
        status = "accepted"
        rel_name = by_name[v].name
        rel_id = by_name[v].context_id
    else:
        raise ResolutionError(
            f"verdict {verdict!r} is neither {NO_RELATION!r} nor a "
            f"mapping_relation ({', '.join(sorted(by_name)) or 'none seeded'})"
        )

    now = datetime.now(timezone.utc)
    db.mappings.update_one(
        {"source_label": source_label, "target_label": target_label},
        {"$set": {
            "status": status,
            "relationship": rel_name,
            "relationship_id": rel_id,
            "rationale": rationale,
            "decided_via": decided_via,
            "decision_rationale": rationale,
            "decided_at": now,
            "is_stale": False,
            "updated_at": now,
        }},
    )
    resolved = repo.get_pair(source_label, target_label)
    assert resolved is not None
    log.info(
        "mappings: resolved %s -> %s: %s (%s) via %s",
        source_label, target_label, status, rel_name, decided_via,
    )
    return resolved


# --- Generation job -----------------------------------------------------------


@dataclass
class MappingGenerationResult:
    source_entries_scanned: int = 0
    candidate_pairs: int = 0
    accepted: int = 0
    rejected: int = 0
    unresolved: int = 0
    errored: int = 0
    attributions: list[MappingAttribution] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "source_entries_scanned": self.source_entries_scanned,
            "candidate_pairs": self.candidate_pairs,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "unresolved": self.unresolved,
            "errored": self.errored,
            "attributions": [a.to_dict() for a in self.attributions],
            "errors": list(self.errors),
        }


def generate_mappings(
    config: AppConfig,
    db: Database,
    adapter,
    *,
    source_language: str,
    target_language: str,
    max_items: int = 20,
    passes: int | None = None,
    apply: bool = False,
    max_target_snapshot: int = 60,
    candidate_source: str | None = None,
    top_k: int | None = None,
) -> MappingGenerationResult:
    """Candidate shortlisting + gated attribution over one language pair.

    Dry-run by default (the I-D pattern): the result carries every
    attribution for operator review; `apply=True` upserts them.
    Pairs that already hold a mapping are skipped (re-audit is the
    staleness path's job, not generation's).

    The shortlist (which target entries to compare against each source)
    comes from one of two stages, per `candidate_source`:
      - "embedding": meaning-closeness over backfilled fingerprints
        (`embeddings.shortlist_candidates`); finds matches by meaning,
        across the whole target lexicon.
      - "prompt": a fast model picks from a snapshot of the target
        lexicon (pre-fingerprint behaviour); capped at `max_target_snapshot`.
      - "auto" (default): embedding when the source has a fingerprint and
        the target language has any, else prompt.
    """
    from mahalath.adapters.base import AdapterError
    from mahalath.embeddings import get_embedding, shortlist_candidates

    entries = OntologyEntryRepository(db)
    mappings = MappingRepository(db)
    effective_passes = passes or config.runtime.intent_consensus_passes
    roster = config.runtime.consensus_models
    source_mode = candidate_source or config.runtime.mapping_candidate_source
    k = top_k or config.runtime.mapping_candidate_top_k

    target_docs = [
        OntologyEntryRepository(db).get(doc["_id"])
        for doc in db.ontology_entries.find(
            {"language": target_language}, {"_id": 1}
        ).limit(max_target_snapshot)
    ]
    target_docs = [t for t in target_docs if t is not None]
    result = MappingGenerationResult()
    if not target_docs:
        return result
    target_labels = {t.mpl_label for t in target_docs}

    def _use_embedding(source_label: str) -> bool:
        if source_mode == "prompt":
            return False
        if source_mode == "embedding":
            return True
        # auto: embeddings when this source has a fingerprint and the
        # target language has at least one to compare against.
        if get_embedding(db, source_label) is None:
            return False
        return db.entry_embeddings.count_documents(
            {"language": target_language}, limit=1
        ) > 0

    source_labels = [
        doc["_id"]
        for doc in db.ontology_entries.find(
            {"language": source_language}, {"_id": 1}
        ).sort("_id", 1).limit(max_items)
    ]

    for source_label in source_labels:
        source = entries.get(source_label)
        if source is None:
            continue
        result.source_entries_scanned += 1

        if _use_embedding(source_label):
            candidate_labels = [
                c.label for c in shortlist_candidates(
                    db, source_label, target_language, top_k=k
                )
            ]
            log.info(
                "mappings: %s embedding-candidates for %s (%s): %s",
                len(candidate_labels), source_label, source.canonical_term,
                candidate_labels or "(none)",
            )
        else:
            try:
                response = adapter.generate(
                    build_candidate_prompt(source, target_docs), want_json=True,
                )
                parsed = _parse_candidates(response.text)
                # prompt models can invent labels → keep only real ones
                # from the snapshot they were shown.
                candidate_labels = [c for c in parsed[:k] if c in target_labels]
                log.info(
                    "mappings: %s prompt-candidates for %s (%s): %s",
                    len(candidate_labels), source_label, source.canonical_term,
                    candidate_labels or "(none)",
                )
            except AdapterError as exc:
                result.errored += 1
                result.errors.append(f"{source_label} candidates: {exc}")
                continue

        for target_label in candidate_labels:
            if mappings.get_pair(source_label, target_label) is not None:
                continue
            result.candidate_pairs += 1
            try:
                attribution = attribute_mapping(
                    db, source_label, target_label, adapter,
                    passes=effective_passes,
                    min_confidence=config.runtime.confidence_threshold,
                    models=roster,
                    apply=apply,
                )
            except AdapterError as exc:
                result.errored += 1
                result.errors.append(
                    f"{source_label}->{target_label}: {exc}"
                )
                continue
            if attribution is None:
                continue
            result.attributions.append(attribution)
            log.info(
                "mappings: %s -> %s: %s (%s, conf %s)",
                source_label, target_label, attribution.status,
                attribution.relationship, attribution.confidence,
            )
            if attribution.status == "accepted":
                result.accepted += 1
            elif attribution.status == "rejected":
                result.rejected += 1
            else:
                result.unresolved += 1
    return result


def _parse_candidates(text: str) -> list[str]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```", 2)
        if len(parts) >= 2:
            inner = parts[1]
            if inner.startswith("json"):
                inner = inner[4:].lstrip()
            cleaned = inner
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            return []
        try:
            obj = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            return []
    if not isinstance(obj, dict):
        return []
    out = []
    for item in obj.get("candidates") or []:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


# --- Staleness participation (ADR-029) ----------------------------------------


def mark_mappings_stale(
    db: Database, changed_label: str, *, change_type: str, note: str | None = None
) -> int:
    """Flag every mapping touching `changed_label` for re-audit."""
    now = datetime.now(timezone.utc)
    result = db.mappings.update_many(
        {
            "$or": [
                {"source_label": changed_label},
                {"target_label": changed_label},
            ],
            "is_stale": False,
        },
        {
            "$set": {"is_stale": True, "updated_at": now},
            "$push": {"stale_reasons": {
                "upstream_label": changed_label,
                "change_type": change_type,
                "changed_at": now,
                "note": note,
            }},
        },
    )
    return result.modified_count
