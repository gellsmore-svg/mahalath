"""Meaning-fingerprints for cross-language candidate shortlisting (M-C).

The first step of making a cross-language mapping is the *shortlist*:
out of every entry in the other language, pick the few worth comparing
to a given entry, so the expensive three-model judging runs on a handful
of pairs rather than all of them. The pre-fingerprint shortlist asked a
fast model to eyeball a list and choose — and it kept missing obvious
matches (e.g. it never offered "coupling" for "Kopplung"), because the
match is a *meaning* match across two languages, which eyeballing a list
does badly.

This module replaces that with meaning-closeness. A multilingual
embedding model (config: bge-m3) turns each entry's definition into a
vector — a fixed list of numbers arranged so two definitions about
similar ideas get similar vectors, *even across languages*. The
shortlist is then mechanical: rank the other language's entries by
closeness to the source and take the nearest few.

Vectors live in their own `entry_embeddings` collection keyed by MPL
label (one current vector per entry), kept out of `ontology_entries` so
ordinary entry reads don't haul large arrays around. Only the backfill
calls the embedding model; shortlisting is pure read + arithmetic, so
mapping generation needs no model running for its candidate stage.

Design notes:
  - The vector is computed from the entry's LATEST definition text, with
    the canonical term prepended (the term itself carries signal).
  - A stored vector records the model and a hash of the text it was made
    from, so a stale vector (definition changed, or model changed) is
    detectable and the backfill recomputes only what it must.
  - Cross-model/cross-dimension vectors are never compared: shortlisting
    filters to one model + matching dimension.
"""

from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pymongo.database import Database

from mahalath.db.repositories import OntologyEntryRepository

log = logging.getLogger("mahalath.embeddings")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def cosine(a: list[float], b: list[float]) -> float:
    """Closeness of two vectors, 1.0 = identical direction, 0.0 =
    unrelated, -1.0 = opposite. Returns 0.0 for a length mismatch or a
    zero vector rather than raising."""
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def embedding_source_text(entry) -> str:
    """The text an entry's fingerprint is computed from: canonical term
    plus its latest definition. Term + definition together embed better
    than the definition alone (the term carries signal)."""
    latest = entry.definitions[-1].text if entry.definitions else ""
    return f"{entry.canonical_term}: {latest}".strip()


def embedding_fallback_text(entry) -> str:
    """A second-choice input when the preferred one trips a model NaN:
    the definition alone, without the term prefix. Empirically dodges
    bge-m3's numerical-instability cases on some inputs (S2.51 live run)."""
    return (entry.definitions[-1].text if entry.definitions else "").strip()


def _source_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- Storage ------------------------------------------------------------------


def get_embedding(db: Database, mpl_label: str) -> dict[str, Any] | None:
    return db.entry_embeddings.find_one({"_id": mpl_label})


def store_embedding(
    db: Database,
    mpl_label: str,
    *,
    language: str,
    model: str,
    vector: list[float],
    source_text: str,
) -> None:
    db.entry_embeddings.replace_one(
        {"_id": mpl_label},
        {
            "_id": mpl_label,
            "language": language,
            "model": model,
            "dim": len(vector),
            "vector": vector,
            "source_hash": _source_hash(source_text),
            "computed_at": _utcnow(),
        },
        upsert=True,
    )


def _is_current(record: dict[str, Any] | None, *, model: str, source_text: str) -> bool:
    return (
        record is not None
        and record.get("model") == model
        and record.get("source_hash") == _source_hash(source_text)
    )


# --- Backfill -----------------------------------------------------------------


@dataclass
class EmbeddingBackfillResult:
    scanned: int = 0
    embedded: int = 0
    embedded_via_fallback: int = 0
    skipped_current: int = 0
    skipped_nan: int = 0
    errored: int = 0
    model: str = ""
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "embedded": self.embedded,
            "embedded_via_fallback": self.embedded_via_fallback,
            "skipped_current": self.skipped_current,
            "skipped_nan": self.skipped_nan,
            "errored": self.errored,
            "model": self.model,
            "errors": list(self.errors),
        }


def backfill_embeddings(
    db: Database,
    adapter,
    *,
    model: str,
    language: str | None = None,
    apply: bool = False,
    recompute_all: bool = False,
) -> EmbeddingBackfillResult:
    """Compute and store a fingerprint for every entry that lacks a
    current one (or all entries when `recompute_all`).

    Dry-run by default (house style): reports what it would embed and
    writes nothing. An entry is "current" when a stored vector matches
    the live definition text and the chosen model — so re-running after
    a re-debate (which changed the text) re-embeds only the changed
    entries.
    """
    from mahalath.adapters.base import AdapterError, EmbeddingNaNError

    result = EmbeddingBackfillResult(model=model)
    query: dict[str, Any] = {} if language is None else {"language": language}
    entries_repo = OntologyEntryRepository(db)

    for doc in db.ontology_entries.find(query, {"_id": 1}).sort("_id", 1):
        entry = entries_repo.get(doc["_id"])
        if entry is None or not entry.definitions:
            continue
        result.scanned += 1
        source_text = embedding_source_text(entry)
        if not recompute_all and _is_current(
            get_embedding(db, entry.mpl_label), model=model, source_text=source_text
        ):
            result.skipped_current += 1
            continue
        if not apply:
            result.embedded += 1  # would embed
            continue

        # Primary input; on a model NaN, retry with the definition alone
        # (dropping the term prefix dodges most of bge-m3's NaN cases),
        # then give up gracefully so a model quirk on a few entries never
        # blocks the rest.
        used_text, resp, via_fallback = source_text, None, False
        try:
            resp = adapter.embed(source_text, model=model)
        except EmbeddingNaNError:
            fallback = embedding_fallback_text(entry)
            if fallback and fallback != source_text:
                try:
                    resp = adapter.embed(fallback, model=model)
                    used_text, via_fallback = fallback, True
                except EmbeddingNaNError:
                    resp = None
            if resp is None:
                result.skipped_nan += 1
                result.errors.append(
                    f"{entry.mpl_label}: non-finite embedding on every input "
                    f"variant (model quirk) — skipped"
                )
                continue
        except AdapterError as exc:
            result.errored += 1
            result.errors.append(f"{entry.mpl_label}: {exc}")
            continue

        store_embedding(
            db, entry.mpl_label,
            language=entry.language, model=resp.model,
            vector=resp.vector, source_text=used_text,
        )
        result.embedded += 1
        if via_fallback:
            result.embedded_via_fallback += 1
        log.info(
            "embeddings: %s (%s) embedded dim=%s via %s%s",
            entry.mpl_label, entry.language, resp.dim, resp.model,
            " [fallback: def-only]" if via_fallback else "",
        )
    return result


# --- Shortlisting -------------------------------------------------------------


@dataclass
class Candidate:
    label: str
    score: float


def shortlist_candidates(
    db: Database,
    source_label: str,
    target_language: str,
    *,
    top_k: int = 5,
    min_score: float = 0.0,
) -> list[Candidate]:
    """The nearest `top_k` target-language entries to `source_label` by
    meaning-closeness. Pure read + arithmetic — no model call.

    Returns [] when the source has no stored vector (caller falls back to
    prompt-based selection). Only vectors from the SAME model and
    dimension as the source are compared.
    """
    src = get_embedding(db, source_label)
    if src is None or not src.get("vector"):
        return []
    src_vec = src["vector"]
    src_model, src_dim = src.get("model"), src.get("dim")

    scored: list[Candidate] = []
    for rec in db.entry_embeddings.find({"language": target_language}):
        if rec["_id"] == source_label:
            continue
        if rec.get("model") != src_model or rec.get("dim") != src_dim:
            continue  # never compare across models/dimensions
        score = cosine(src_vec, rec["vector"])
        if score >= min_score:
            scored.append(Candidate(label=rec["_id"], score=round(score, 6)))

    scored.sort(key=lambda c: c.score, reverse=True)
    return scored[:top_k]
