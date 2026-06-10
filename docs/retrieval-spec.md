# Mahalath Retrieval Layer — Design Spec

Last updated: 2026-06-10
Status: accepted design. S-A landed (S2.30); S-B..S-E pending.
Related: ADR-018, ADR-019, ADR-020, ADR-021, ADR-022, ADR-023.

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
   a raw tree dump (operator NFR). Budget trims *breadth* (how many
   candidate terms / alternatives) and verbosity — it never collapses
   the frames of a confidently-matched term (see principle 6).
6. **Retrieval surfaces all frames; the caller disambiguates (ADR-022).**
   For a matched human term, retrieval returns *every* codified
   meaning (one per frame), with provenance. It does not pick "the"
   meaning. Choosing which frame(s) are relevant is the consuming LLM's
   job, because only the consumer holds the question/context that
   resolves the polysemy.
7. **Returned meanings are reference-closed (ADR-023).** A description
   written in codified terms is only interpretable if every codified
   term it cites is also resolvable. So whenever a meaning's description
   references other MPL labels, those entries are included in the bundle
   too — transitively, until the set is closed, with a visited-set cycle
   guard. The closure is mandatory: budget pressure degrades the
   *fidelity* of deep nodes (compact label + active meaning instead of
   full provenance) but never drops a referenced codified term.

## Consumer model

The same call serves two roles, and the caller decides how much to keep:

- **Retrieval-LLM (pre-pruner).** Calls retrieval to pare context: reads
  the full frame set, keeps the relevant meanings, forwards only those to
  a downstream answer-LLM. Efficient context-window management.
- **Answer-LLM (direct).** Calls retrieval itself and reasons over the
  full set in one shot. Simpler, less context-efficient.

Implication for the API: `build_bundle` defaults to returning all frames
per matched term. A retrieval-LLM that has selected a subset can re-issue
`get_codified`/`build_bundle` against the specific `(mpl_label,
context_id)` handles it chose — retrieval stays read-only; selection
state lives with the caller, not in Mahalath.

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
    Compose a prompt-ready bundle. Resolves terms via search_terms, then
    for each matched term includes ALL of its frame meanings (retrieval
    never collapses polysemy — ADR-022). token_budget trims breadth
    (how many candidate terms / ranked alternatives) and verbosity, not
    the frame set of a matched term. Emits both JSON and a compact NL
    rendering. A caller that has already chosen specific
    (mpl_label, context_id) handles can pass those as refs to get just
    those meanings back.
    Reference closure (ADR-023): every MPL label cited inside an included
    meaning's description is resolved and added to the bundle,
    transitively, with a cycle guard. Closure nodes beyond the primary
    matches are rendered compactly (label + active meaning) under budget
    but are always present.

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
  `meanings: list[{context_id, context_name, description, model_used,
  consensus_score, created_at}]`, `references`, `referenced_by`,
  `document_labels`, `is_stale`, `stale_reasons`. Each meaning's
  `(mpl_label, context_id)` is the stable handle a caller keeps or
  forwards; `context_name` is its readable label. All frames are
  present — retrieval does not pre-select (ADR-022).
- `Bundle`: `entries: list[CodifiedRef]` (the primary matches),
  `closure: list[CodifiedRef-compact]` (transitively-referenced terms
  pulled in to make every description self-contained — ADR-023),
  `alternatives`, `token_estimate`, `as_json`, `as_text` (compact NL).
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

1. **S-A — `retrieval.py` core. DONE (S2.30).** Shared `score_entry`
   ranker (extracted from `chat.py`, which now calls it), `search_terms`
   (substring + `$text` fuzzy, with branch/context/status/min-confidence
   filters), `get_codified` (all frames, on-the-fly `path`, references +
   reverse-references), the `$text` index, and `mahalath retrieve` CLI.
   `path` computed by walking `parent_label` (denormalised field deferred
   to S-B).
2. **S-B — `path` field + backfill.** Add `OntologyEntry.path`,
   maintain on insert/reparent, one-shot `backfill-paths` migration,
   `subtree()`.
3. **S-C — `build_bundle` + `/api/retrieve` HTTP.** Token-budgeted
   bundles, JSON + compact NL; reference-closure expansion (ADR-023)
   with cycle guard; wire chat's context block through it.
4. **S-D — `propose_term`** onto the existing undecided/ingestion path;
   optional `consensus_score` capture during debate.
5. **S-E (optional, later) — Tirzah semantic backend** behind
   `search_terms` for fuzzy/semantic recall. Cross-project; needs an
   explicit go-ahead (ADR-014).

## Resolved questions

- **Q1 — RESOLVED (2026-06-10, operator).** Retrieval does not pick a
  frame, so there is no single forced prose-citation form to choose. It
  returns all frame meanings; each is keyed by `(mpl_label, context_id)`
  and labelled with `context_name`. The consuming LLM decides which to
  keep, and may cite them however it (or its downstream answer-LLM)
  prefers — typically `MPL-004` plus the frame label it kept. Recorded as
  ADR-022 ("retrieval surfaces all frames; the caller disambiguates").

## Open questions

- **Q2.** `token_budget` accounting: rough char/4 estimate in core, or a
  real tokenizer per adapter? Lean: char-based estimate in core, exact
  count optional via the active adapter.
- **Q3.** Should `build_bundle` ever auto-trigger `propose_term` when a
  term misses, or always leave that to the caller? Lean: caller-driven
  (retrieval stays read-only by default).
- **Q4.** Does the operator want a stable *external* alias for an MPL
  label (a human-pronounceable handle) without violating ADR-021? If so
  it is an `alias`, not a new key.
- **Q5 — RESOLVED (2026-06-10).** Closure uses *entry-level*
  `references_labels` for v1. Over-inclusion (pulling in a term
  referenced only by a discarded frame) is safe and cheaper than
  per-frame tracking. Per-`DefinitionVersion` reference tracking is a
  later refinement, added only if over-inclusion proves noisy in
  practice.
