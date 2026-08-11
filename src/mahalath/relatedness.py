"""Link related source documents and correspond their terms (ADR-036).

**This is not deduplication.** An incoming document is processed in full and
the original document's terms are left untouched. What is added is a recorded
relationship, so that terms arising from a related document are traceable to
the older terms they correspond to.

The purpose is comparison: run a different model, or a changed process, over
related material and see how the resulting term set differs from the one
already held. Nothing else in the system can currently answer "did that change
improve the output?".

Two deliberate choices, both from ADR-036:

* **An LLM judges relatedness, not a text diff.** A revised edition, a
  reformatted export or a translated chapter is *related* in the sense that
  matters here even when its bytes share very little; conversely two documents
  can share boilerplate and be unrelated. There is no mechanical fast path for
  byte-identical documents because that case cannot arise: ingestion rejects a
  repeated checksum (ADR-016) and `documents.checksum_sha256` is uniquely
  indexed, so two identical documents never both exist to be compared.
* **Terms correspond, they do not merge.** A correspondence records that
  MPL-A (from document 1) and MPL-B (from document 2) name the same meaning as
  far as the judge could tell. Neither entry is modified.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pymongo.database import Database

from mahalath.adapters.base import Adapter, AdapterError

log = logging.getLogger("mahalath.relatedness")

# How a link was established.
BY_MODEL = "model"         # an LLM judged the documents related
BY_OPERATOR = "operator"   # a person asserted the link

RELATION_KINDS: frozenset[str] = frozenset(
    {"same_work", "revision", "translation", "excerpt", "shares_material"}
)

# Characters of each document shown to the judge. Enough to recognise a work
# without paying to send whole books through a local model.
_SAMPLE_CHARS = 3000


class RelatednessError(Exception):
    """Raised when a relatedness judgement cannot be used."""


@dataclass
class DocumentLink:
    """An asserted relationship between two ingested documents."""

    link_id: str
    document_id: str
    related_document_id: str
    relation: str
    confidence: float
    established_by: str
    rationale: str = ""
    model_used: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "link_id": self.link_id,
            "document_id": self.document_id,
            "related_document_id": self.related_document_id,
            "relation": self.relation,
            "confidence": self.confidence,
            "established_by": self.established_by,
            "rationale": self.rationale,
            "model_used": self.model_used,
            "created_at": self.created_at,
        }


@dataclass
class TermCorrespondence:
    """Two entries, from linked documents, judged to name the same meaning."""

    correspondence_id: str
    link_id: str
    mpl_label: str
    related_mpl_label: str
    document_id: str
    related_document_id: str
    confidence: float
    established_by: str
    note: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "correspondence_id": self.correspondence_id,
            "link_id": self.link_id,
            "mpl_label": self.mpl_label,
            "related_mpl_label": self.related_mpl_label,
            "document_id": self.document_id,
            "related_document_id": self.related_document_id,
            "confidence": self.confidence,
            "established_by": self.established_by,
            "note": self.note,
            "created_at": self.created_at,
        }


# --- judging ---------------------------------------------------------------


def _sample(db: Database, document_id: str) -> tuple[str, str]:
    """(title, opening text) for a document; empty strings when unavailable."""
    from pathlib import Path

    record = db.documents.find_one({"document_id": document_id}) or {}
    title = str(record.get("title") or "")
    for key in ("archive_path", "source_path"):
        raw = record.get(key)
        if not raw:
            continue
        path = Path(str(raw))
        try:
            if path.exists():
                return title, path.read_text(
                    encoding="utf-8", errors="replace"
                )[:_SAMPLE_CHARS]
        except OSError:
            continue
    return title, ""


def build_relatedness_prompt(
    incoming: tuple[str, str], candidate: tuple[str, str]
) -> str:
    """Ask whether two documents are related, and how."""
    return "\n".join([
        "You are comparing two source documents for a lexicon builder.",
        "",
        "Decide whether they are RELATED — the same work, a revision or new "
        "edition, a translation, an excerpt, or sharing substantial material. "
        "Two documents merely on the same topic are NOT related. Judge the "
        "documents, not the subject matter.",
        "",
        f"DOCUMENT A — {incoming[0]!r}",
        incoming[1],
        "",
        f"DOCUMENT B — {candidate[0]!r}",
        candidate[1],
        "",
        "Reply with ONLY a JSON object:",
        '{"related": true|false, '
        '"relation": "same_work|revision|translation|excerpt|shares_material", '
        '"confidence": 0.0-10.0, "rationale": "one sentence"}',
        "If they are not related, set related=false and relation to null.",
    ])


def parse_relatedness_verdict(text: str) -> dict[str, Any]:
    """Parse the judge's reply; raise RelatednessError on anything unusable."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        if len(parts) >= 2:
            raw = parts[1]
            if raw.lstrip().lower().startswith("json"):
                raw = raw.lstrip()[4:]
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise RelatednessError("no JSON object in relatedness reply")
    try:
        parsed = json.loads(raw[start : end + 1])
    except ValueError as exc:
        raise RelatednessError(f"unparseable relatedness reply: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RelatednessError("relatedness reply is not an object")

    related = bool(parsed.get("related"))
    relation = parsed.get("relation")
    if related:
        if relation not in RELATION_KINDS:
            raise RelatednessError(
                f"unknown relation {relation!r} (allowed: {sorted(RELATION_KINDS)})"
            )
    else:
        relation = None
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError) as exc:
        raise RelatednessError("confidence must be a number") from exc
    if not 0.0 <= confidence <= 10.0:
        raise RelatednessError(f"confidence {confidence} outside 0-10")
    return {
        "related": related,
        "relation": relation,
        "confidence": confidence,
        "rationale": str(parsed.get("rationale") or "").strip(),
    }


def find_related_documents(
    db: Database,
    document_id: str,
    adapter: Adapter,
    *,
    min_confidence: float = 6.0,
    max_candidates: int = 20,
    model: str | None = None,
) -> list[DocumentLink]:
    """Judge an incoming document against those already processed.

    Byte-identical documents are linked without asking a model — that case is
    certain. Everything else is put to the judge. Returns the links written.
    """
    incoming = db.documents.find_one({"document_id": document_id})
    if incoming is None:
        raise RelatednessError(f"no document {document_id!r}")

    candidates = list(
        db.documents.find({"document_id": {"$ne": document_id}}).limit(max_candidates)
    )
    if not candidates:
        return []

    incoming_sample = _sample(db, document_id)
    links: list[DocumentLink] = []
    for candidate in candidates:
        other_id = candidate.get("document_id")
        if not other_id or _link_exists(db, document_id, other_id):
            continue

        prompt = build_relatedness_prompt(
            incoming_sample, _sample(db, other_id)
        )
        try:
            response = adapter.generate(prompt, model=model, want_json=True)
            verdict = parse_relatedness_verdict(response.text)
        except (AdapterError, RelatednessError) as exc:
            log.info(
                "relatedness: skipped %s vs %s — %s", document_id, other_id, exc
            )
            continue

        if not verdict["related"] or verdict["confidence"] < min_confidence:
            continue
        links.append(
            _write_link(
                db, document_id, other_id,
                relation=verdict["relation"],
                confidence=verdict["confidence"],
                established_by=BY_MODEL,
                rationale=verdict["rationale"],
                model_used=model or getattr(adapter, "default_model", None),
            )
        )
    return links


def _link_exists(db: Database, a: str, b: str) -> bool:
    return db.document_links.count_documents(
        {
            "$or": [
                {"document_id": a, "related_document_id": b},
                {"document_id": b, "related_document_id": a},
            ]
        },
        limit=1,
    ) > 0


def _write_link(
    db: Database,
    document_id: str,
    related_document_id: str,
    *,
    relation: str,
    confidence: float,
    established_by: str,
    rationale: str = "",
    model_used: str | None = None,
) -> DocumentLink:
    link = DocumentLink(
        link_id=str(uuid4()),
        document_id=document_id,
        related_document_id=related_document_id,
        relation=relation,
        confidence=confidence,
        established_by=established_by,
        rationale=rationale,
        model_used=model_used,
    )
    db.document_links.insert_one(link.to_dict())
    log.info(
        "relatedness: linked %s ↔ %s (%s, %s, conf %.1f)",
        document_id, related_document_id, relation, established_by, confidence,
    )
    return link


# --- term correspondence ---------------------------------------------------


def correspond_terms(
    db: Database, link_id: str, *, established_by: str = BY_MODEL
) -> list[TermCorrespondence]:
    """Match terms across two linked documents, without modifying either.

    Correspondence is by canonical term (case-folded) plus alias overlap. That
    is deliberately conservative: the point is to line up two term sets for
    comparison, so a missed pair shows as a difference to look at, while a
    wrong pair would quietly claim two meanings are the same.
    """
    link = db.document_links.find_one({"link_id": link_id})
    if link is None:
        raise RelatednessError(f"no link {link_id!r}")

    left = _terms_for_document(db, link["document_id"])
    right = _terms_for_document(db, link["related_document_id"])

    out: list[TermCorrespondence] = []
    for key, entry in left.items():
        other = right.get(key)
        if other is None or entry["_id"] == other["_id"]:
            continue
        if db.term_correspondences.count_documents(
            {
                "link_id": link_id,
                "mpl_label": entry["_id"],
                "related_mpl_label": other["_id"],
            },
            limit=1,
        ):
            continue
        correspondence = TermCorrespondence(
            correspondence_id=str(uuid4()),
            link_id=link_id,
            mpl_label=entry["_id"],
            related_mpl_label=other["_id"],
            document_id=link["document_id"],
            related_document_id=link["related_document_id"],
            confidence=float(link.get("confidence") or 0.0),
            established_by=established_by,
            note=f"matched on {key!r}",
        )
        db.term_correspondences.insert_one(correspondence.to_dict())
        out.append(correspondence)
    return out


def _terms_for_document(db: Database, document_id: str) -> dict[str, dict[str, Any]]:
    """{case-folded term or alias → entry} for entries evidenced by a document."""
    index: dict[str, dict[str, Any]] = {}
    for doc in db.ontology_entries.find({"source_document_ids": document_id}):
        for name in [doc.get("canonical_term"), *(doc.get("aliases") or [])]:
            key = str(name or "").strip().casefold()
            if key:
                index.setdefault(key, doc)
    return index


def compare_linked_documents(db: Database, link_id: str) -> dict[str, Any]:
    """What differs between two linked documents' term sets.

    This is the payoff of ADR-036: after re-running a corpus with a different
    model or a changed process, this says what the change actually did —
    which terms are shared, which are only in one side, and how the shared
    ones' definitions differ.
    """
    link = db.document_links.find_one({"link_id": link_id})
    if link is None:
        raise RelatednessError(f"no link {link_id!r}")

    left = _terms_for_document(db, link["document_id"])
    right = _terms_for_document(db, link["related_document_id"])
    shared = sorted(set(left) & set(right))

    differing: list[dict[str, Any]] = []
    for key in shared:
        a, b = left[key], right[key]
        a_text = (a.get("definitions") or [{}])[-1].get("text", "")
        b_text = (b.get("definitions") or [{}])[-1].get("text", "")
        if a_text.strip() != b_text.strip():
            differing.append({
                "term": key,
                "mpl_label": a["_id"],
                "related_mpl_label": b["_id"],
                "definition": a_text,
                "related_definition": b_text,
            })

    titles = {
        d["document_id"]: d.get("title") or d["document_id"]
        for d in db.documents.find(
            {"document_id": {"$in": [link["document_id"], link["related_document_id"]]}},
            {"document_id": 1, "title": 1},
        )
    }
    return {
        "link_id": link_id,
        "document_id": link["document_id"],
        "document_title": titles.get(link["document_id"], ""),
        "related_document_id": link["related_document_id"],
        "related_document_title": titles.get(link["related_document_id"], ""),
        "relation": link.get("relation"),
        "shared_terms": len(shared),
        "only_in_document": sorted(set(left) - set(right)),
        "only_in_related": sorted(set(right) - set(left)),
        "differing_definitions": differing,
    }
