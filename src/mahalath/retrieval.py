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

S-C adds the prompt-ready layer:

  - `build_bundle` — resolve terms and/or explicit MPL refs into a
    token-budgeted `Bundle`: primary `CodifiedRef`s with ALL their
    frames, a mandatory reference closure (ADR-023, cycle-safe), ranked
    alternatives, and a compact NL rendering (`as_text`).
  - `render_entry_lines` — the ONE text renderer shared by bundle
    output and the chat context block, so retrieval text and chat
    context look identical to a downstream model.
  - Token accounting is a chars/4 estimate (retrieval-spec Q2 lean).
    Budget pressure trims breadth and verbosity in recorded steps
    (`Bundle.degradations`); it NEVER collapses the frame set of an
    included entry and NEVER drops a closure node.

Retrieval never collapses polysemy (ADR-022): `get_codified` returns
every frame; the caller disambiguates.
"""

from __future__ import annotations

import json
import math
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


def _label_mentioned(label_cf: str, query_cf: str) -> bool:
    """True if the exact MPL label appears in `query_cf` as a whole token.

    Plain substring matching over-matches the label grammar: `MPL-001`
    is a prefix of both `MPL-001.001` (child) and `MPL-001a` (variant),
    and `\\b` alone doesn't help because `.` is itself a word boundary.
    So reject a match that continues with `.<digit>` (a child segment)
    or an alphanumeric (a variant suffix / longer number).
    """
    return re.search(
        r"\b" + re.escape(label_cf) + r"(?!\.\d)(?![a-z0-9])", query_cf
    ) is not None


# --- Shared relevance scorer ----------------------------------------------


def score_entry(entry: OntologyEntry, query_cf: str) -> int:
    """Score one entry against a case-folded query string.

    The shared ranking core used by both `chat.select_context_entries`
    and `search_terms`:

        +30 a direct MPL-label mention (whole token — `MPL-001` does
            not match inside `MPL-001.001` or `MPL-001a`)
        +20 a canonical_term word-boundary match (>= 4 chars)
        +15 any alias word-boundary match (>= 4 chars)
    """
    score = 0
    if _label_mentioned(entry.mpl_label.casefold(), query_cf):
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
    # Intent annotation (I-C surfaces what I-B stored; ADR-024/025).
    # `intent_tags` carries readable taxonomy NAMES; raw ids live on
    # the underlying DefinitionVersion.
    intent_tags: list[str] = field(default_factory=list)
    intentionality: str | None = None
    intent_confidence: float | None = None

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
    # Which language lexicon to search (ADR-030: explicit parameter,
    # "en" default). Label-addressed reads (get_codified, subtree) are
    # unaffected — labels are globally unique.
    language: str = "en"
    branch: str | None = None          # ancestor MPL label must be on the path
    context_name: str | None = None    # entry must carry a definition in this frame
    status: str | None = None
    min_confidence: float | None = None
    intent_tag: str | None = None      # entry must carry a definition tagged
                                       # with this intent (name or context_id)


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

    # Resolve an intent-tag filter once (name or id → context_id). An
    # unknown / frame name matches nothing — fail closed, not open.
    intent_tag_id: str | None = None
    if filters is not None and filters.intent_tag:
        from mahalath.intents import resolve_intent_tag
        intent_tag_id = resolve_intent_tag(db, filters.intent_tag)
        if intent_tag_id is None:
            return []

    language = filters.language if filters is not None else "en"
    text_hits = _text_search(db, " ".join(terms), language=language)

    scored: list[Match] = []
    # One bulk fetch (was: all labels + one get() round-trip per label).
    from mahalath.db.models import OntologyEntry as _Entry

    for doc in db.ontology_entries.find({"language": language}):
        entry = _Entry.model_validate(doc)
        label = entry.mpl_label
        base = score_entry(entry, query_cf)
        text_bonus = text_hits.get(label, 0.0)
        total = base + int(round(text_bonus * 10))
        if total <= 0:
            continue
        if not _passes(db, entry, filters, ctx_by_id, intent_tag_id):
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


def _text_search(
    db: Database, query: str, *, language: str = "en"
) -> dict[str, float]:
    """Run the Mongo `$text` query; return {mpl_label: textScore}.

    Returns an empty map (not an error) when the text index is absent so
    retrieval degrades to scorer-only on un-indexed databases.
    """
    try:
        cursor = db.ontology_entries.find(
            {"$text": {"$search": query}, "language": language},
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
    intent_tag_id: str | None = None,
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
    if intent_tag_id is not None:
        if not any(
            intent_tag_id in d.intent_tags for d in entry.definitions
        ):
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
    # Frames only: a `MPL-x#<frame>` handle names a meaning frame, never
    # an intent-taxonomy row (ADR-024).
    ctx_by_id = {c.context_id: c for c in ctx_repo.all(kind="frame")}
    ctx_by_name = {c.name: c for c in ctx_by_id.values()}
    # Intent ids → readable names for the Meaning surface (I-C).
    intent_name_by_id = {
        c.context_id: c.name for c in ctx_repo.all(kind="intent")
    }

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
            consensus_score=d.consensus_score,
            created_at=d.created_at.isoformat() if d.created_at else None,
            intent_tags=[
                intent_name_by_id.get(t, t) for t in d.intent_tags
            ],
            intentionality=d.intentionality,
            intent_confidence=d.intent_confidence,
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

    Fast path: one multikey query on the materialised `path` field
    (`{"path": label}` matches every descendant); a node's depth is its
    position past `label` in its own path. Falls back to a per-level
    `parent_label` walk when the database still has un-backfilled legacy
    paths (an entry with a parent but an empty path), so results stay
    correct pre-migration. Returns None for an unknown root.
    """
    repo = OntologyEntryRepository(db)
    if repo.get(label) is None:
        return None
    ctx_by_id = {c.context_id: c.name for c in DefinitionContextRepository(db).all()}

    if _has_unmaterialised_paths(db):
        found = _subtree_walk(repo, label, depth)
    else:
        found = []
        for doc in db.ontology_entries.find({"path": label}):
            entry = OntologyEntry.model_validate(doc)
            node_depth = len(entry.path) - entry.path.index(label)
            if node_depth <= depth:
                found.append((node_depth, entry))

    found.sort(key=lambda pair: (pair[0], pair[1].mpl_label))
    nodes: list[SubtreeNode] = []
    for node_depth, entry in found:
        frames = sorted({
            name
            for d in entry.definitions
            if d.context_id
            for name in [ctx_by_id.get(d.context_id, d.context_id)]
            if name
        })
        nodes.append(SubtreeNode(
            mpl_label=entry.mpl_label,
            canonical_term=entry.canonical_term,
            depth=node_depth,
            parent_label=entry.parent_label,
            frames=frames,
            is_stale=entry.is_stale,
        ))

    return SubtreeSummary(root=label, depth=depth, nodes=nodes, count=len(nodes))


def _has_unmaterialised_paths(db: Database) -> bool:
    """True if any entry has a parent but no stored path (pre-migration)."""
    return db.ontology_entries.count_documents(
        {
            "parent_label": {"$ne": None},
            "$or": [{"path": {"$size": 0}}, {"path": {"$exists": False}}],
        },
        limit=1,
    ) > 0


def _subtree_walk(
    repo: OntologyEntryRepository, label: str, depth: int
) -> list[tuple[int, OntologyEntry]]:
    """Legacy per-level `parent_label` walk (cycle-safe via visited set)."""
    found: list[tuple[int, OntologyEntry]] = []
    seen: set[str] = {label}
    frontier = [label]
    for level in range(1, depth + 1):
        next_frontier: list[str] = []
        for parent in frontier:
            for child_label in repo.labels_under_parent(parent):
                if child_label in seen:
                    continue
                seen.add(child_label)
                child = repo.get(child_label)
                if child is None:
                    continue
                found.append((level, child))
                next_frontier.append(child_label)
        frontier = next_frontier
        if not frontier:
            break
    return found


# --- Shared text rendering (S-C) -------------------------------------------


def render_entry_lines(
    *,
    mpl_label: str,
    canonical_term: str,
    meanings: list[Meaning],
    parent_label: str | None = None,
    path: list[str] | None = None,
    references: list[str] | None = None,
    aliases: list[str] | None = None,
    is_stale: bool = False,
    stale_reasons: list[dict] | None = None,
    context_descriptions: dict[str, str] | None = None,
    compact: bool = False,
    provenance: bool = True,
    closure_text_cap: int = 160,
) -> list[str]:
    """Render one entry as prompt text. The ONE renderer.

    Both the bundle's `as_text` and the chat context block go through
    here, so a downstream model sees the same idiom everywhere (MPL
    label primary, frame-grouped, co-equal frames called out).

    `compact=True` is the closure-node fidelity: label + per-frame
    meaning one-liners (capped at `closure_text_cap` chars), no
    provenance/tree detail. `provenance=False` keeps the full shape but
    drops per-definition attribution (a budget degradation step).
    `context_descriptions` maps context NAME -> description.
    """
    lines: list[str] = [f"--- {mpl_label}: {canonical_term} ---"]

    if compact:
        for m in meanings:
            frame = m.context_name or "unspecified"
            text = m.description.strip()
            if len(text) > closure_text_cap:
                text = text[: closure_text_cap - 1].rstrip() + "…"
            lines.append(f"  [{frame}] {text}")
        if is_stale:
            lines.append("  (STALE)")
        return lines

    lines.append(f"  Parent: {parent_label or '(top-level)'}")
    if path:
        lines.append(f"  Path: {' > '.join(path)}")
    if is_stale:
        reasons = stale_reasons or []
        lines.append(f"  STALE (reasons: {len(reasons)})")
        for r in reasons[-2:]:
            note = r.get("note") or ""
            change_type = r.get("change_type", "?")
            lines.append(f"    - {change_type}: {note}")

    if meanings:
        distinct_ctx = {m.context_id for m in meanings}
        if len(distinct_ctx) > 1:
            lines.append(
                "  Definitions (multiple contexts are co-equal — each speaks "
                "from its own frame; none supersedes another):"
            )
        else:
            lines.append("  Definitions:")
        # Group by frame, preserving first-appearance order.
        order: list[str | None] = []
        groups: dict[str | None, list[Meaning]] = {}
        for m in meanings:
            if m.context_id not in groups:
                groups[m.context_id] = []
                order.append(m.context_id)
            groups[m.context_id].append(m)
        for cid in order:
            group = groups[cid]
            frame = group[0].context_name
            if frame:
                desc = (context_descriptions or {}).get(frame)
                if desc:
                    lines.append(f"    Context [{frame}] — {desc}")
                else:
                    lines.append(f"    Context [{frame}]")
            else:
                lines.append("    Context [unspecified]")
            for m in group:
                if provenance:
                    lines.append(f"      [{m.model_used or '?'}] {m.description}")
                else:
                    lines.append(f"      {m.description}")
                # Intent annotation (deployment metadata, ADR-024) —
                # rendered as an aside so it never reads as semantics.
                extras = []
                if m.intent_tags:
                    extras.append("deployed to: " + ", ".join(m.intent_tags))
                if m.intentionality:
                    extras.append(f"intentionality: {m.intentionality}")
                if extras:
                    lines.append(f"        ({'; '.join(extras)})")

    if references:
        lines.append(f"  References: {', '.join(references[:8])}")
    if aliases:
        lines.append(f"  Aliases: {', '.join(aliases[:5])}")
    return lines


def render_codified_lines(
    ref: CodifiedRef,
    *,
    context_descriptions: dict[str, str] | None = None,
    compact: bool = False,
    provenance: bool = True,
    closure_text_cap: int = 160,
) -> list[str]:
    """`render_entry_lines` adapter for a CodifiedRef."""
    return render_entry_lines(
        mpl_label=ref.mpl_label,
        canonical_term=ref.canonical_term,
        meanings=ref.meanings,
        parent_label=ref.parent_label,
        path=ref.path or None,
        references=ref.references,
        aliases=ref.aliases,
        is_stale=ref.is_stale,
        stale_reasons=ref.stale_reasons,
        context_descriptions=context_descriptions,
        compact=compact,
        provenance=provenance,
        closure_text_cap=closure_text_cap,
    )


def estimate_tokens(text: str) -> int:
    """Char-based token estimate (~4 chars/token; retrieval-spec Q2 lean)."""
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


# --- Bundles (S-C) ----------------------------------------------------------


@dataclass
class BundleAlternative:
    """A ranked match that did not make the primary set."""

    mpl_label: str
    canonical_term: str
    score: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Bundle:
    """A prompt-ready, reference-closed, token-budgeted answer set.

    `entries` are the primary matches (every frame present, ADR-022).
    `closure` is the transitive set of MPL labels referenced by any
    included entry (ADR-023) — rendered compactly in `as_text` but never
    dropped. `degradations` records which budget trims were applied so
    the caller can see why the output is shaped the way it is.
    """

    entries: list[CodifiedRef]
    closure: list[CodifiedRef]
    alternatives: list[BundleAlternative]
    unresolved: list[str]
    token_estimate: int
    token_budget: int
    degradations: list[str]
    as_text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": [e.to_dict() for e in self.entries],
            "closure": [c.to_dict() for c in self.closure],
            "alternatives": [a.to_dict() for a in self.alternatives],
            "unresolved": list(self.unresolved),
            "token_estimate": self.token_estimate,
            "token_budget": self.token_budget,
            "degradations": list(self.degradations),
            "as_text": self.as_text,
        }

    def as_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


def build_bundle(
    db: Database,
    refs_or_terms: list[str],
    *,
    token_budget: int = 1500,
    filters: Filters | None = None,
    limit_per_term: int = 5,
) -> Bundle:
    """Compose a prompt-ready bundle from human terms and/or MPL refs.

    Inputs that parse as MPL refs (`MPL-x`, `MPL-x#<frame>`) are
    expanded directly — these are caller-chosen handles and are never
    dropped under budget. Everything else is resolved via
    `search_terms`; the top match per term becomes a primary (with ALL
    frames, ADR-022) and the rest become ranked alternatives.

    Reference closure (ADR-023): every label cited by an included entry
    is pulled in transitively (entry-level references, Q5), cycle-safe.

    Budget (chars/4 estimate, Q2): degradation steps are applied in
    order until the rendering fits — cap alternatives, drop provenance
    detail, trim the lowest-scored term-derived primaries (moved to
    alternatives, never below one primary), and finally harden the
    closure one-liners. The frame set of an included entry and the
    closure node set are NEVER reduced. Steps applied are recorded in
    `Bundle.degradations`.
    """
    ctx_descriptions = {
        c.name: c.description for c in DefinitionContextRepository(db).all()
    }

    explicit: list[str] = []
    terms: list[str] = []
    for item in (s.strip() for s in refs_or_terms):
        if not item:
            continue
        (explicit if _MPL_REF.match(item) else terms).append(item)

    unresolved: list[str] = []
    primaries: list[tuple[int | None, CodifiedRef]] = []  # (score, ref)
    seen_primary: set[str] = set()

    # Caller-chosen handles first; never dropped under budget.
    for ref_str in explicit:
        ref = get_codified(db, ref_str)
        if ref is None:
            unresolved.append(ref_str)
            continue
        key = ref_str  # frame-scoped handles are distinct from full refs
        if key in seen_primary:
            continue
        seen_primary.add(key)
        primaries.append((None, ref))

    explicit_count = len(primaries)

    # Term resolution: top match per term -> primary; rest -> alternatives.
    alternatives: list[BundleAlternative] = []
    seen_alt: set[str] = set()
    for term in terms:
        matches = search_terms(db, [term], filters=filters, limit=limit_per_term)
        if not matches:
            unresolved.append(term)
            continue
        top, rest = matches[0], matches[1:]
        if top.mpl_label not in seen_primary:
            ref = get_codified(db, top.mpl_label)
            if ref is not None:
                seen_primary.add(top.mpl_label)
                primaries.append((top.score, ref))
        for m in rest:
            if m.mpl_label in seen_primary or m.mpl_label in seen_alt:
                continue
            seen_alt.add(m.mpl_label)
            alternatives.append(BundleAlternative(
                mpl_label=m.mpl_label,
                canonical_term=m.canonical_term,
                score=m.score,
            ))

    degradations: list[str] = []
    provenance = True
    closure_text_cap = 160
    max_alternatives: int | None = None

    def _assemble() -> tuple[list[CodifiedRef], str, int]:
        refs = [ref for _, ref in primaries]
        closure = _reference_closure(db, refs)
        shown_alts = (
            alternatives if max_alternatives is None
            else alternatives[:max_alternatives]
        )
        text = _render_bundle_text(
            refs, closure, shown_alts,
            ctx_descriptions=ctx_descriptions,
            provenance=provenance,
            closure_text_cap=closure_text_cap,
        )
        return closure, text, estimate_tokens(text)

    closure, text, tokens = _assemble()

    # Degradation ladder — each step re-renders and re-measures.
    if tokens > token_budget and len(alternatives) > 3:
        max_alternatives = 3
        degradations.append("alternatives_capped")
        closure, text, tokens = _assemble()

    if tokens > token_budget:
        provenance = False
        degradations.append("provenance_dropped")
        closure, text, tokens = _assemble()

    # Trim term-derived primaries (lowest score first); explicit handles
    # and the final remaining primary are never dropped.
    while tokens > token_budget and len(primaries) > max(1, explicit_count):
        scored = [
            (i, score) for i, (score, _) in enumerate(primaries)
            if score is not None
        ]
        if not scored:
            break
        drop_i = min(scored, key=lambda pair: pair[1])[0]
        _, dropped = primaries.pop(drop_i)
        alternatives.insert(0, BundleAlternative(
            mpl_label=dropped.mpl_label,
            canonical_term=dropped.canonical_term,
            score=0,
        ))
        if "breadth_trimmed" not in degradations:
            degradations.append("breadth_trimmed")
        closure, text, tokens = _assemble()

    if tokens > token_budget and closure_text_cap > 80:
        closure_text_cap = 80
        degradations.append("closure_hardened")
        closure, text, tokens = _assemble()

    shown_alts = (
        alternatives if max_alternatives is None
        else alternatives[:max_alternatives]
    )
    return Bundle(
        entries=[ref for _, ref in primaries],
        closure=closure,
        alternatives=shown_alts,
        unresolved=unresolved,
        token_estimate=tokens,
        token_budget=token_budget,
        degradations=degradations,
        as_text=text,
    )


def _reference_closure(
    db: Database, primaries: list[CodifiedRef]
) -> list[CodifiedRef]:
    """Transitively expand references of `primaries` (ADR-023).

    Entry-level granularity (Q5), visited-set cycle guard, BFS so
    nearer references render first. Dangling references (a label cited
    in text but absent from the DB) are skipped silently — they cannot
    be resolved, so they cannot be closed over.
    """
    visited: set[str] = {ref.mpl_label for ref in primaries}
    queue: list[str] = []
    for ref in primaries:
        for cited in ref.references:
            if cited not in visited:
                visited.add(cited)
                queue.append(cited)

    closure: list[CodifiedRef] = []
    i = 0
    while i < len(queue):
        label = queue[i]
        i += 1
        ref = get_codified(db, label)
        if ref is None:
            continue
        closure.append(ref)
        for cited in ref.references:
            if cited not in visited:
                visited.add(cited)
                queue.append(cited)
    return closure


def _render_bundle_text(
    primaries: list[CodifiedRef],
    closure: list[CodifiedRef],
    alternatives: list[BundleAlternative],
    *,
    ctx_descriptions: dict[str, str],
    provenance: bool,
    closure_text_cap: int,
) -> str:
    """The bundle's compact NL rendering (same idiom as chat context)."""
    lines: list[str] = [
        "CODIFIED MEANINGS",
        "",
        "Frames are co-equal: when an entry carries several, choose the "
        "frame(s) your question implies; cite the MPL label (and frame "
        "name) you keep.",
        "",
    ]
    for ref in primaries:
        lines.extend(render_codified_lines(
            ref,
            context_descriptions=ctx_descriptions,
            provenance=provenance,
        ))
        lines.append("")

    if closure:
        lines.append("REFERENCED TERMS (closure — cited by the entries above)")
        lines.append("")
        for ref in closure:
            lines.extend(render_codified_lines(
                ref, compact=True, closure_text_cap=closure_text_cap,
            ))
        lines.append("")

    if alternatives:
        alts = ", ".join(
            f"{a.mpl_label} ({a.canonical_term})" for a in alternatives
        )
        lines.append(f"OTHER CANDIDATE MATCHES: {alts}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# --- propose_term (S-D) -----------------------------------------------------


@dataclass
class ProposalTemplate:
    """Structured outcome of a propose_term call.

    `status` is one of:
      existing        — a confident match already covers this term; no
                        enqueue happened (matches carry the candidates).
      already_queued  — the term is already in the undecided queue;
                        decision_log_id points at the existing row.
      enqueued        — a new undecided-queue row was created; the term
                        will flow through the normal REM re-review /
                        operator path like any inconclusive debate.
      template_only   — no confident match and enqueue=False; nothing
                        was written (dry-run).
    """

    term: str
    status: str
    matches: list[Match]
    context: str | None
    near: str | None
    enqueued: bool
    decision_log_id: str | None
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "term": self.term,
            "status": self.status,
            "matches": [m.to_dict() for m in self.matches],
            "context": self.context,
            "near": self.near,
            "enqueued": self.enqueued,
            "decision_log_id": self.decision_log_id,
            "note": self.note,
        }


def propose_term(
    db: Database,
    term: str,
    *,
    context: str | None = None,
    near: str | None = None,
    enqueue: bool = True,
    min_score: int = 20,
    language: str = "en",
) -> ProposalTemplate:
    """Propose a term the ontology doesn't confidently cover yet.

    The ONE write path in this module (everything else is a read view).
    When `search_terms` finds a confident match (score >= `min_score` —
    the canonical-term word-boundary weight by default), nothing is
    written and the matches are returned for the caller to use instead.
    Otherwise the term is enqueued onto the EXISTING undecided path: a
    minimal DecisionLogEntry (outcome "undecided", so the queue row's
    decision_log_id resolves like every other) plus an UndecidedItem
    with reason "proposed_term". REM re-review will pick it up and
    debate it like any inconclusive term — provided a `context` snippet
    is supplied (context-less rows are operator-only by design).

    `near` (optional) is an MPL label the proposer believes the term
    relates to; it is resolved to its canonical term and folded into
    the debate context as a semantic hint.

    Per Q3 (resolved): `build_bundle` never calls this automatically —
    callers decide, using `Bundle.unresolved` as their signal.
    """
    term = term.strip()
    if not term:
        raise ValueError("propose_term requires a non-empty term")

    matches = search_terms(
        db, [term], filters=Filters(language=language), limit=5
    )
    confident = [m for m in matches if m.score >= min_score]
    if confident:
        return ProposalTemplate(
            term=term, status="existing", matches=matches,
            context=context, near=near, enqueued=False,
            decision_log_id=None,
            note=(
                f"{len(confident)} confident match(es) already cover this "
                "term; use the matches instead of proposing."
            ),
        )

    existing = db.undecided_queue.find_one(
        {"term": {"$regex": f"^{re.escape(term)}$", "$options": "i"}}
    )
    if existing is not None:
        return ProposalTemplate(
            term=term, status="already_queued", matches=matches,
            context=context, near=near, enqueued=False,
            decision_log_id=existing.get("decision_log_id"),
            note="term is already in the undecided queue",
        )

    if not enqueue:
        return ProposalTemplate(
            term=term, status="template_only", matches=matches,
            context=context, near=near, enqueued=False,
            decision_log_id=None,
            note="no confident match; pass enqueue=True to queue it",
        )

    # Fold the `near` hint into the debate context as a semantic cue
    # (the debate speaks in terms, not labels — resolve it).
    debate_context = context
    if near:
        near_entry = OntologyEntryRepository(db).get(near)
        if near_entry is not None:
            hint = (
                f"(The proposer suggests this concept is related to "
                f"\"{near_entry.canonical_term}\".)"
            )
            debate_context = f"{context}\n{hint}" if context else hint

    from mahalath.db.models import DecisionLogEntry, UndecidedItem
    from mahalath.db.repositories import (
        DecisionLogRepository,
        UndecidedQueueRepository,
    )

    log_entry = DecisionLogEntry(
        term=term,
        source_document_id="(proposed-via-retrieval)",
        outcome="undecided",
    )
    DecisionLogRepository(db).insert(log_entry)
    UndecidedQueueRepository(db).insert(UndecidedItem(
        decision_log_id=log_entry.decision_log_id,
        term=term,
        source_document_id="(proposed-via-retrieval)",
        reason="proposed_term",
        context=debate_context,
        last_confidence=None,
    ))

    note = "enqueued onto the undecided path"
    if not debate_context:
        note += (
            " WITHOUT context — REM re-review skips context-less rows, so "
            "this item waits for an operator; supply context to make it "
            "auto-debatable."
        )
    return ProposalTemplate(
        term=term, status="enqueued", matches=matches,
        context=debate_context, near=near, enqueued=True,
        decision_log_id=log_entry.decision_log_id,
        note=note,
    )
