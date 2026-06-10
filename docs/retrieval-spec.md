# Mahalath Retrieval Layer — Design Spec

Last updated: 2026-06-10
Status: accepted design; not yet implemented (see "Phasing").
Related: ADR-018, ADR-019, ADR-020, ADR-021.

## Purpose

Let an orchestrating LLM (via Python, CLI, or HTTP) query the ontology
for **precise, codified meanings** and get back a compact, prompt-ready
bundle — so the consuming model can reason in Mahalath's machine-native
terms (MPL labels + frames) instead of ambiguous English.

This reconciles an operator-supplied retrieval spec with the schema and
surfaces Mahalath already has. The headline finding: **most of the spec
already exists under different names.** Retrieval is therefore a *read
view* over the current collections plus a small typed API — not a new
store.

## Reconciliation with the operator spec

| Operator spec term | Existing Mahalath reality |
|---|---|
| `codified_label` | **MPL label** (`labels.py`), used as `_id` of `ontology_entries`. Stable, opaque, immutable (ADR-018). |
| `human_label` (+ variants) | `canonical_term` + `aliases` |
| `parent_id` / tree | `parent_label` (denormalised) + `ontology_tree` edges |
| `meanings[]` (versioned) | `definitions: list[DefinitionVersion]` — append-only; each has `text`, `model_used`, `context_id`, `created_at` |
| per-meaning `contexts` | `DefinitionContext` objects referenced by `definitions[].context_id` (the structural/theological/physical frames) |
| `variants` (splits/reparents) | re-parenting stores `previous_parent_label`; splits create child entries; `decision_log` + `stale_reasons` hold the audit chain |
| `llm_consensus` provenance | `model_used` per definition, `decision_log_id` → debate transcript in `agent_exchanges`, entry `confidence`, consensus passes |
| new-term/branch proposal | `undecided_queue` + `action_proposals` + `frontier-review` + the PrecisionCritic/SynthesisExplorer debate |
| structured JSON / compact NL output | `glossary.v1` export, `/api/chat` JSON, `build_chat_prompt` context-grouped text |
| LLM-driven selective retrieval | `chat.answer_question` already selects + ranks relevant entries and builds a context-grouped prompt |
| indexes on label/term/parent/refs | all present in `db/indexes.py` |

Two operator-spec ideas are **declined** and the reasons recorded as
ADRs:

- **No parallel node schema** (ADR-020). The proposed
  `codified_label`/`parent_id`/`path`/`meanings`/`variants` document
  duplicates collections that already exist. Retrieval reads the current
  schema; only two additive fields are permitted (below).
- **No semantics baked into the label** (ADR-021).
  `MAH_BANK_FIN_20250610_V3` contradicts ADR-018/019. The codified
  reference is the opaque MPL label (or `(MPL, context_id)` for a
  specific frame); domain/version/frame are returned as *fields*.

One operator-spec constraint is **relaxed**: "no lateral traversal."
Mahalath already has `references_labels` (lateral links mined from
definition text) and a reverse index, and chat's 1-hop neighbourhood
retrieval depends on them. They are indexed and cheap; retrieval keeps
them.

## Design principles

1. **Retrieval is a read view.** It does not own storage; it composes
   `OntologyEntryRepository`, `DefinitionContextRepository`,
   `OntologyTreeRepository`, and the staleness reverse-index.
2. **The codified reference is the MPL label.** A frame-specific
   reference is the pair `(mpl_label, context_id)`. Everything else
   (domain, version, provenance) is a returned field.
3. **One ranking core.** Factor chat's entry-selection scorer out of
   `chat.py` into a shared ranker so chat and retrieval cannot drift.
4. **Embeddings live in Tirzah.** Mahalath core does exact + `$text`
   fuzzy matching. Semantic/vector similarity is delegated to Tirzah
   when needed (ADR-014; standing rule: codex owns Tirzah). The API is
   shaped so a semantic backend can be slotted behind `search_terms`
   later without changing callers.
5. **Bundles are budgeted.** Output is pruned to a token budget — never
   a raw tree dump (operator NFR).

## Additive schema changes (the only ones permitted, per ADR-020)

1. `OntologyEntry.path: list[str]` — ancestor MPL labels from root to
   this entry's parent (the materialised path). Maintained on insert and
   on re-parent. Enables fast subtree/branch queries and a tree-context
   summary without `$graphLookup`. Backfillable for existing DBs from
   `ontology_tree` (one-shot migration, mirrors `backfill_references`).
2. `DefinitionVersion.consensus_score: float | None` — an optional
   consolidated agreement score for that specific definition (distinct
   from the entry-level `confidence`). Written when the debate that
   produced the definition recorded multi-agent agreement; `None` for
   legacy/operator definitions.

A `$text` index on `ontology_entries` over `canonical_term`, `aliases`,
and `definitions.text` (weighted term > alias > definition) backs fuzzy
matching. No vector index in core.

## API surface (`src/mahalath/retrieval.py`)

Typed, side-effect-free reads (except `propose_term`, which enqueues).

```
search_terms(db, terms, *, filters=None, limit=20) -> list[Match]
    Resolve one or more human terms/phrases to candidate entries.
    Exact word-boundary + $text fuzzy, scored by the shared ranker.
    filters: branch (path prefix), context_name, status, min_confidence,
             updated_after, document_label.

get_codified(db, ref) -> CodifiedRef | None
    ref is an MPL label (or "MPL-004#<context_id>" for one frame).
    Returns: active meaning(s) grouped by frame, full provenance,
    path summary, references + reverse-references, document labels.

build_bundle(db, refs_or_terms, *, token_budget=1500, context=None)
        -> Bundle
    Compose a prompt-ready bundle: best match + ranked alternatives per
    input, pruned to token_budget. Emits both JSON and a compact NL
    rendering. Resolves terms via search_terms first.

subtree(db, label, *, depth=1) -> SubtreeSummary
    Limited-depth descendant summary using path/edges (operator FR-5).

propose_term(db, term, *, context=None, near=None) -> ProposalTemplate
    When search_terms finds no confident match, return a structured
    "no good match" template and (optionally) enqueue it onto the
    existing undecided/ingestion path.
```

### Return shapes (dataclasses, JSON-serialisable)

- `Match`: `mpl_label`, `canonical_term`, `score`, `match_kind`
  (`exact|alias|text|label`), `frames: list[str]`, `is_stale`.
- `CodifiedRef`: `mpl_label`, `canonical_term`, `aliases`,
  `path: list[str]`, `parent_label`,
  `meanings: list[{context_name, description, model_used,
  consensus_score, created_at}]`, `references`, `referenced_by`,
  `document_labels`, `is_stale`, `stale_reasons`.
- `Bundle`: `entries: list[CodifiedRef-lite]`, `alternatives`,
  `token_estimate`, `as_json`, `as_text` (compact NL).
- `SubtreeSummary`, `ProposalTemplate`: as named.

The compact NL rendering reuses the `build_chat_prompt` idiom (MPL label
primary, frame-grouped), so retrieval text and chat context look the
same to a downstream model.

## Surfaces

- **Library** — `from mahalath.retrieval import build_bundle` etc.
- **CLI** — `mahalath retrieve <terms...> [--branch L] [--context C]
  [--budget N] [--format json|text]`. Prints the bundle.
- **HTTP** — `POST /api/retrieve` `{terms|labels, filters, token_budget,
  format}` → bundle JSON. Sits beside `/api/chat` in `web/app.py`.

`answer_question` (chat) is refactored to call the shared ranker and may
optionally call `build_bundle` to construct its context block, so chat
becomes one consumer of retrieval rather than a parallel implementation.

## Non-functional targets (from the operator spec)

- Sub-second targeted lookups: exact/`$text` are index-backed;
  `build_bundle` caps the candidate set before any per-entry work.
- Scale: adjacency-list + materialised `path`; no graph DB (ADR-003).
- Provenance + confidence always included in every returned meaning.
- Drift mitigation: outputs lead with the MPL label and ask the
  consuming model to cite it (ADR-019), not the English term.

## Phasing (suggested slices)

1. **S-A — `retrieval.py` core.** Shared ranker (extracted from
   `chat.py`), `search_terms`, `get_codified`, `$text` index,
   `mahalath retrieve` CLI. Smallest thing that lets an LLM query and get
   codified refs back. No schema change yet (path/score deferred).
2. **S-B — `path` field + backfill.** Add `OntologyEntry.path`,
   maintain on insert/reparent, one-shot `backfill-paths` migration,
   `subtree()`.
3. **S-C — `build_bundle` + `/api/retrieve` HTTP.** Token-budgeted
   bundles, JSON + compact NL; wire chat's context block through it.
4. **S-D — `propose_term`** onto the existing undecided/ingestion path;
   optional `consensus_score` capture during debate.
5. **S-E (optional, later) — Tirzah semantic backend** behind
   `search_terms` for fuzzy/semantic recall. Cross-project; needs an
   explicit go-ahead (ADR-014).

## Open questions

- **Q1.** Frame-specific references in prose: is `MPL-004#<context_id>`
  the right surface form for "the theological substrate," or should the
  pair be expressed some other way an LLM cites cleanly?
- **Q2.** `token_budget` accounting: rough char/4 estimate in core, or a
  real tokenizer per adapter? Lean: char-based estimate in core, exact
  count optional via the active adapter.
- **Q3.** Should `build_bundle` ever auto-trigger `propose_term` when a
  term misses, or always leave that to the caller? Lean: caller-driven
  (retrieval stays read-only by default).
- **Q4.** Does the operator want a stable *external* alias for an MPL
  label (a human-pronounceable handle) without violating ADR-021? If so
  it is an `alias`, not a new key.
