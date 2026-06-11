"""Programmatic retrieval over the ontology (S-A of docs/retrieval-spec.md).

A read view that lets an orchestrating LLM resolve human terms to
codified MPL references and pull back the full, frame-preserving meaning
of each — so the consuming model can reason in Mahalath's machine-native
terms instead of ambiguous English.

This slice (S-A) ships the query core:

  - `score_entry` — the shared relevance scorer, extracted here so the
    chat backend and retrieval cannot drift (ADR: one ranking core).
  - `search_terms` — resolve term(s)/phrase(s) to ranked `Match`es,
    combining exact/alias/label scoring with the `$text` fuzzy index.
  - `get_codified` — expand an MPL ref (optionally `MPL-x#<context_id>`
    for one frame) into a `CodifiedRef`: all frame meanings, provenance,
    tree path, references + reverse-references, document labels.

S-B adds `subtree` (limited-depth descendant summary) and switches path
resolution to the denormalised `OntologyEntry.path` (maintained in
`paths.py`), falling back to a live walk for un-backfilled legacy
entries.

Retrieval never collapses polysemy (ADR-022): `get_codified` returns
every frame; the caller disambiguates. Reference closure and budgeted
bundles arrive in a later slice (S-C).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from pymongo.database import Database
from pymongo.errors import OperationFailure

from mahalath.db.models import OntologyEntry
from mahalath.db.repositories import (
    DefinitionContextRepository,
    OntologyEntryRepository,
)
from mahalath.paths import resolved_path

_MIN_TERM_LEN = 4
_MPL_REF = re.compile(r"^(?P<label>MPL-\d{3}(?:\.\d{3})*[a-z]?)(?:#(?P<ctx>.+))?$")


# --- Shared relevance scorer ----------------------------------------------


def score_entry(entry: OntologyEntry, query_cf: str) -> int:
    """Score one entry against a case-folded query string.

    The shared ranking core used by both `chat.select_context_entries`
    and `search_terms`:

        +30 a direct MPL-label mention
        +20 a canonical_term word-boundary match (>= 4 chars)
        +15 any alias word-boundary match (>= 4 chars)
    """
    score = 0
    if entry.mpl_label.casefold() in query_cf:
        score += 30
    term_cf = entry.canonical_term.casefold() if entry.canonical_term else ""
    if len(term_cf) >= _MIN_TERM_LEN and re.search(
        r"\b" + re.escape(term_cf) + r"\b", query_cf
    ):
        score += 20
    for alias in entry.aliases:
        alias_cf = alias.casefold()
        if len(alias_cf) >= _MIN_TERM_LEN and re.search(
            r"\b" + re.escape(alias_cf) + r"\b", query_cf
        ):
            score += 15
    return score


def _match_kind(entry: OntologyEntry, terms_cf: list[str], via_text: bool) -> str:
    """Strongest reason this entry matched, for caller transparency."""
    if any(entry.mpl_label.casefold() == t for t in terms_cf):
        return "label"
    if entry.canonical_term and entry.canonical_term.casefold() in terms_cf:
        return "exact"
    if any(a.casefold() in terms_cf for a in entry.aliases):
        return "alias"
    return "text" if via_text else "partial"


# --- Result shapes --------------------------------------------------------


@dataclass
class Match:
    mpl_label: str
    canonical_term: str
    score: int
    match_kind: str           # label | exact | alias | partial | text
    frames: list[str]         # context names present on this entry
    is_stale: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Meaning:
    context_id: str | None
    context_name: str | None   # readable label for the frame
    description: str
    model_used: str | None
    consensus_score: float | None
    created_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CodifiedRef:
    mpl_label: str
    canonical_term: str
    aliases: list[str]
    path: list[str]            # ancestor MPL labels, root-first
    parent_label: str | None
    meanings: list[Meaning]
    references: list[str]      # MPL labels this entry cites
    referenced_by: list[str]   # MPL labels that cite this entry
    document_labels: list[str]
    is_stale: bool
    stale_reasons: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


# --- Filters --------------------------------------------------------------


@dataclass
class Filters:
    branch: str | None = None          # ancestor MPL label must be on the path
    context_name: str | None = None    # entry must carry a definition in this frame
    status: str | None = None
    min_confidence: float | None = None


# --- Search ---------------------------------------------------------------


def search_terms(
    db: Database,
    terms: list[str],
    *,
    filters: Filters | None = None,
    limit: int = 20,
) -> list[Match]:
    """Resolve human term(s)/phrase(s) to ranked ontology matches.

    Combines the substring/word-boundary scorer with the `$text` fuzzy
    index (the latter catches stemmed / multi-word hits the substring
    scorer misses). Falls back to scorer-only if no text index exists.
    """
    repo = OntologyEntryRepository(db)
    ctx_by_id = {c.context_id: c.name for c in DefinitionContextRepository(db).all()}
    terms = [t for t in (s.strip() for s in terms) if t]
    if not terms:
        return []
    query_cf = " ".join(terms).casefold()
    terms_cf = [t.casefold() for t in terms]

    text_hits = _text_search(db, " ".join(terms))

    scored: list[Match] = []
    for label in repo.all_labels():
        entry = repo.get(label)
        if entry is None:
            continue
        base = score_entry(entry, query_cf)
        text_bonus = text_hits.get(label, 0.0)
        total = base + int(round(text_bonus * 10))
        if total <= 0:
            continue
        if not _passes(db, entry, filters, ctx_by_id):
            continue
        frames = sorted({
            ctx_by_id.get(d.context_id, d.context_id)
            for d in entry.definitions if d.context_id
        })
        scored.append(Match(
            mpl_label=entry.mpl_label,
            canonical_term=entry.canonical_term,
            score=total,
            match_kind=_match_kind(entry, terms_cf, via_text=label in text_hits),
            frames=[f for f in frames if f],
            is_stale=entry.is_stale,
        ))

    scored.sort(key=lambda m: (m.score, m.mpl_label), reverse=True)
    return scored[:limit]


def _text_search(db: Database, query: str) -> dict[str, float]:
    """Run the Mongo `$text` query; return {mpl_label: textScore}.

    Returns an empty map (not an error) when the text index is absent so
    retrieval degrades to scorer-only on un-indexed databases.
    """
    try:
        cursor = db.ontology_entries.find(
            {"$text": {"$search": query}},
            {"score": {"$meta": "textScore"}},
        ).limit(100)
        return {doc["_id"]: doc.get("score", 0.0) for doc in cursor}
    except OperationFailure:
        return {}


def _passes(
    db: Database,
    entry: OntologyEntry,
    filters: Filters | None,
    ctx_by_id: dict[str, str],
) -> bool:
    if filters is None:
        return True
    if filters.status is not None and entry.status != filters.status:
        return False
    if filters.min_confidence is not None and entry.confidence < filters.min_confidence:
        return False
    if filters.context_name is not None:
        names = {ctx_by_id.get(d.context_id) for d in entry.definitions if d.context_id}
        if filters.context_name not in names:
            return False
    if filters.branch is not None:
        path = resolved_path(db, entry)
        if filters.branch not in path and filters.branch != entry.mpl_label:
            return False
    return True


# --- Codified expansion ---------------------------------------------------


def get_codified(db: Database, ref: str) -> CodifiedRef | None:
    """Expand an MPL ref into its full codified meaning.

    `ref` is an MPL label, or `MPL-x#<context_id>` / `MPL-x#<frame_name>`
    to restrict the returned meanings to a single frame. Unknown label or
    unknown frame returns None.
    """
    m = _MPL_REF.match(ref.strip())
    if not m:
        return None
    label = m.group("label")
    ctx_filter = m.group("ctx")

    repo = OntologyEntryRepository(db)
    entry = repo.get(label)
    if entry is None:
        return None

    ctx_repo = DefinitionContextRepository(db)
    ctx_by_id = {c.context_id: c for c in ctx_repo.all()}
    ctx_by_name = {c.name: c for c in ctx_by_id.values()}

    # Resolve a frame filter (by id or readable name) to a context_id.
    keep_ctx_id: str | None = None
    if ctx_filter is not None:
        if ctx_filter in ctx_by_id:
            keep_ctx_id = ctx_filter
        elif ctx_filter in ctx_by_name:
            keep_ctx_id = ctx_by_name[ctx_filter].context_id
        else:
            return None  # asked for a frame this entry's contexts can't name

    meanings: list[Meaning] = []
    for d in entry.definitions:
        if keep_ctx_id is not None and d.context_id != keep_ctx_id:
            continue
        ctx = ctx_by_id.get(d.context_id) if d.context_id else None
        meanings.append(Meaning(
            context_id=d.context_id,
            context_name=ctx.name if ctx else None,
            description=d.text,
            model_used=d.model_used,
            consensus_score=getattr(d, "consensus_score", None),
            created_at=d.created_at.isoformat() if d.created_at else None,
        ))
    if keep_ctx_id is not None and not meanings:
        return None  # frame exists globally but this entry has no def in it

    from mahalath.staleness import entries_referencing
    referenced_by = [e.mpl_label for e in entries_referencing(db, label)]

    return CodifiedRef(
        mpl_label=entry.mpl_label,
        canonical_term=entry.canonical_term,
        aliases=list(entry.aliases),
        path=resolved_path(db, entry),
        parent_label=entry.parent_label,
        meanings=meanings,
        references=list(entry.references_labels),
        referenced_by=referenced_by,
        document_labels=list(entry.source_document_ids),
        is_stale=entry.is_stale,
        stale_reasons=list(entry.stale_reasons),
    )


# --- Subtree summary ------------------------------------------------------


@dataclass
class SubtreeNode:
    mpl_label: str
    canonical_term: str
    depth: int                 # 1 = direct child of the root
    parent_label: str | None
    frames: list[str]
    is_stale: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SubtreeSummary:
    root: str
    depth: int                 # the requested depth cap
    nodes: list[SubtreeNode]   # descendants, breadth-first by depth
    count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def subtree(db: Database, label: str, *, depth: int = 1) -> SubtreeSummary | None:
    """Limited-depth descendant summary rooted at `label` (operator FR-5).

    Walks the denormalised `parent_label` breadth-first to `depth`
    levels (depth=1 → direct children). Cycle-safe via a visited set.
    Returns None for an unknown root.
    """
    repo = OntologyEntryRepository(db)
    if repo.get(label) is None:
        return None
    ctx_by_id = {c.context_id: c.name for c in DefinitionContextRepository(db).all()}

    nodes: list[SubtreeNode] = []
    seen: set[str] = {label}
    frontier = [label]
    for level in range(1, depth + 1):
        next_frontier: list[str] = []
        for parent in frontier:
            for child_label in sorted(repo.labels_under_parent(parent)):
                if child_label in seen:
                    continue
                seen.add(child_label)
                child = repo.get(child_label)
                if child is None:
                    continue
                frames = sorted({
                    name
                    for d in child.definitions
                    if d.context_id
                    for name in [ctx_by_id.get(d.context_id, d.context_id)]
                    if name
                })
                nodes.append(SubtreeNode(
                    mpl_label=child.mpl_label,
                    canonical_term=child.canonical_term,
                    depth=level,
                    parent_label=child.parent_label,
                    frames=frames,
                    is_stale=child.is_stale,
                ))
                next_frontier.append(child_label)
        frontier = next_frontier
        if not frontier:
            break

    return SubtreeSummary(root=label, depth=depth, nodes=nodes, count=len(nodes))
