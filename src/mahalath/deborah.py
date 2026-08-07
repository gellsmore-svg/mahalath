"""Deborah estate adapters — Mahalath novel-concept / term resolution.

Used by the substrate slice to detect unmodelled concepts before critique.
Deborah does not hard-depend on Mahalath; this module is imported when present.
"""

from __future__ import annotations

import re
from typing import Any, Callable

CapabilityDispatch = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]

_TERM_RE = re.compile(
    r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+|[a-z]{4,}(?:[-_][a-z]{3,})*)\b"
)
_STOP = frozenset(
    {
        "that",
        "this",
        "with",
        "from",
        "have",
        "been",
        "were",
        "will",
        "would",
        "could",
        "should",
        "about",
        "which",
        "their",
        "there",
        "where",
        "when",
        "what",
        "does",
        "into",
        "over",
        "under",
        "well",
        "supported",
        "local",
        "corpus",
        "claim",
        "whether",
        "relational",  # keep if ontology has it; still scored
    }
)


def extract_candidate_terms(text: str, *, min_len: int = 4) -> list[str]:
    """Heuristic term harvest for novel-concept detection."""
    text = (text or "").strip()
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for m in _TERM_RE.finditer(text):
        term = m.group(1).strip()
        key = term.casefold()
        if len(term) < min_len or key in _STOP or key in seen:
            continue
        seen.add(key)
        found.append(term)
    # Also multi-word lower phrases already captured; ensure claim keywords
    for word in re.findall(r"[A-Za-z][A-Za-z-]{3,}", text):
        key = word.casefold()
        if key in _STOP or key in seen or len(word) < min_len:
            continue
        seen.add(key)
        found.append(word)
    return found[:24]


def classify_matches(
    terms: list[str],
    matches_by_term: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Split terms into known vs novel given retrieval match dicts."""
    known: list[dict[str, Any]] = []
    novel: list[str] = []
    for term in terms:
        hits = matches_by_term.get(term) or matches_by_term.get(term.casefold()) or []
        confident = [
            h
            for h in hits
            if str(h.get("match_kind") or "") in {"label", "exact", "alias"}
        ]
        if confident:
            best = confident[0]
            known.append(
                {
                    "term": term,
                    "mpl_label": best.get("mpl_label"),
                    "canonical_term": best.get("canonical_term"),
                    "match_kind": best.get("match_kind"),
                }
            )
        elif hits:
            # fuzzy only → treat as weak known with residual
            best = hits[0]
            known.append(
                {
                    "term": term,
                    "mpl_label": best.get("mpl_label"),
                    "canonical_term": best.get("canonical_term"),
                    "match_kind": best.get("match_kind"),
                    "weak": True,
                }
            )
        else:
            novel.append(term)
    return {
        "terms_checked": list(terms),
        "known": known,
        "novel": novel,
        "novel_detected": bool(novel),
        "criteria": ["concept_coverage", "ontology_grounding"],
        "scores": {
            "concept_coverage": "low" if novel else "high",
            "ontology_grounding": "low" if novel else "medium",
        },
        "objections": (
            [f"unmodelled concept(s): {', '.join(novel[:8])}"] if novel else []
        ),
        "confidence": {
            "evidence": "medium" if known else "low",
            "inference": "low" if novel else "medium",
            "execution": "high",
            "basis": "mahalath.search_terms",
        },
    }


def detect_novel_concepts(
    text: str,
    *,
    db: Any = None,
    search_fn: Callable[[list[str]], list[Any]] | None = None,
    terms: list[str] | None = None,
) -> dict[str, Any]:
    """Detect terms in ``text`` that lack confident ontology matches.

    Prefer injectable ``search_fn(terms) -> list[Match|dict]`` for tests.
    With ``db``, uses :func:`mahalath.retrieval.search_terms`.
    """
    candidates = terms if terms is not None else extract_candidate_terms(text)
    if not candidates:
        return classify_matches([], {})

    matches_by_term: dict[str, list[dict[str, Any]]] = {t: [] for t in candidates}

    if search_fn is not None:
        raw = search_fn(candidates) or []
        for m in raw:
            d = m if isinstance(m, dict) else {
                "mpl_label": getattr(m, "mpl_label", None),
                "canonical_term": getattr(m, "canonical_term", None),
                "match_kind": getattr(m, "match_kind", None),
                "frames": getattr(m, "frames", None),
                "is_stale": getattr(m, "is_stale", False),
                "query_term": getattr(m, "query_term", None),
            }
            # Attach to best matching candidate
            ct = str(d.get("canonical_term") or "").casefold()
            qt = str(d.get("query_term") or "").casefold()
            placed = False
            for t in candidates:
                if t.casefold() == ct or t.casefold() == qt or t.casefold() in ct:
                    matches_by_term[t].append(d)
                    placed = True
                    break
            if not placed and candidates:
                matches_by_term[candidates[0]].append(d)
    elif db is not None:
        from mahalath.retrieval import search_terms

        for t in candidates:
            hits = search_terms(db, [t], limit=5)
            matches_by_term[t] = [
                {
                    "mpl_label": h.mpl_label,
                    "canonical_term": h.canonical_term,
                    "match_kind": h.match_kind,
                    "frames": list(h.frames or []),
                    "is_stale": bool(h.is_stale),
                }
                for h in hits
            ]
    else:
        # No ontology — all candidates novel (honest open path)
        return classify_matches(candidates, {t: [] for t in candidates})

    return classify_matches(candidates, matches_by_term)


def make_novel_concept_handler(
    *,
    db: Any = None,
    search_fn: Callable[[list[str]], list[Any]] | None = None,
    residual_on_novel: bool = True,
) -> CapabilityDispatch:
    """Estate dispatch: evaluate-shaped product; residual when novel terms found."""

    def handler(step: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        text = str(
            context.get("claim")
            or context.get("request")
            or context.get("query")
            or step.get("purpose")
            or ""
        )
        product = detect_novel_concepts(text, db=db, search_fn=search_fn)
        # Stash for infer step
        context.setdefault("novel_terms", product.get("novel") or [])
        out: dict[str, Any] = {"status": "completed", "result": product}
        if residual_on_novel and product.get("novel_detected"):
            out["residual"] = True
            out["reason"] = "novel concepts: " + ", ".join(product["novel"][:8])
        return out

    return handler


def deborah_dispatch(
    *,
    db: Any = None,
    search_fn: Callable[[list[str]], list[Any]] | None = None,
) -> dict[str, CapabilityDispatch]:
    h = make_novel_concept_handler(db=db, search_fn=search_fn)
    return {
        "mahalath.retrieve": h,
        "mahalath.detect_novel": h,
        "detect_novel_concept": h,
        "detect_novel": h,
    }


def capability_index_entries() -> dict[str, dict[str, Any]]:
    return {
        "mahalath.retrieve": {
            "name": "mahalath.retrieve",
            "product": "mahalath",
            "kind": "tool",
            "tags": ["semantic", "ontology"],
        },
        "mahalath.detect_novel": {
            "name": "mahalath.detect_novel",
            "product": "mahalath",
            "kind": "tool",
            "tags": ["semantic", "novel", "deborah"],
        },
    }
