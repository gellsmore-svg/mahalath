# Semantic Intent Extension — Evaluation Against the Existing Architecture

Last updated: 2026-06-11
Status: evaluation complete; **awaiting operator decision** on DQ-013/DQ-014.
Source proposal: `docs/intent-extension-discussion.md` (operator-supplied,
filed verbatim).
Related: ADR-019, ADR-020, ADR-021, ADR-022, DQ-005; S2.25
(`_INTENT_VALENCE_GUIDANCE` in `debate.py`).

## Headline finding

About half of the proposal already exists in Mahalath, philosophically
and partially in code:

- **"Context is the condition under which locution acquires illocution"
  is the `DefinitionContext` design.** The proposal's "Fine." example
  (one locution, four meanings depending on context) is exactly what
  `(mpl_label, context_id)` pairs model today, including ADR-022's
  "retrieval surfaces all frames; the caller disambiguates".
- **Intent and valence are already debated** — S2.25 added
  `_INTENT_VALENCE_GUIDANCE` to both agent preambles: capture *why the
  corpus deploys a concept* and *how it positions it*. Deliberately in
  **prose**, not as structured fields.

The proposal therefore reduces to one product decision: **promote
illocution from prose guidance to structured, queryable storage.**

## Scope boundary (accepted as proposed)

The proposal's own recommendation — operate at locution + illocution,
defer interpretation + perlocution — is right, and one consequence
should be made explicit:

**The proposal's `semantic_instance` document must NOT be built as
written.** It is utterance-level (a locution span + its inferred
intent); Mahalath's unit is the *concept*. Utterance-level instances
are memory/passage work — Tirzah's territory (ADR-014: codex owns
Tirzah; runtime independence). What Mahalath can absorb is the
*concept-level* residue of the idea: per-definition intent annotation.

## Evaluation by task

### 1. Architecture changes

None structural. This is the `DefinitionContext` playbook re-run:

- a small operator-governed taxonomy (intent tags),
- an optional annotation on `DefinitionVersion`,
- a backfill command,
- a retrieval filter.

No new subsystem, no new agent role, no pipeline reshaping.

### 2. Schema changes (additive only, per ADR-020)

- `DefinitionContext` gains `kind: "frame" | "intent"` (default
  `"frame"`). One governed-taxonomy mechanism, not two parallel ones.
  Intent tags are operator-authored rows with `kind="intent"`
  (e.g. `teach`, `persuade`, `restore`, `warn`, `consecrate`).
- `DefinitionVersion` gains:
  - `intent_tags: list[str]` (taxonomy context_ids, default `[]`),
  - `intentionality: "low" | "medium" | "high" | None`,
  - `intent_confidence: float | None` (0–10 scale, matching ADR-010).
- **Ordinal intentionality, not floats.** The proposal's `0.83` is
  pseudo-precision: gemma4:e2b cannot meaningfully discriminate 0.83
  from 0.79 (the S2.4 direction-variance evidence — confident 8.0+ in
  *both* directions — is this project's own proof). Coarse honesty
  beats fine-grained noise.
- **Never in the label** (ADR-021 already forbids it) and **never a new
  entry axis** (draft ADR-024 below).

### 3. Retrieval implications

Composes cleanly with the S-A/S-B layer:

- `Filters` gains `intent_tag`.
- `Meaning` gains `intent_tags` / `intentionality` / `intent_confidence`.
- ADR-022 generalises: retrieval surfaces all frames *and* all recorded
  intents; the caller disambiguates.
- "Find concepts intended to encourage ownership" becomes an indexed
  filter query once tags are stored.
- Intent should NOT alter default ranking (open question in the
  proposal): rank by lexical relevance, *filter* by intent. An
  intent-boosted ranking mode can be a later opt-in.

### 4. Ingestion implications

- Extend the debate output contract with optional `intent_tags` +
  `intentionality` (the S2.23 `context_name` pattern), null-tolerant.
- Scoped backfill at pipeline tail (the S2.27 pattern) catches
  definitions the debate left unannotated.
- `frontier-review` adjudicates low-confidence attributions.
- **Reliability is the open empirical question** (the proposal asks
  "can intentionality be inferred reliably enough?"). Local-model
  intent attribution will be noisy; the existing mitigations are
  multi-pass consensus (S2.5 machinery, tag-identity instead of
  action-identity — store only unanimous tags) and confidence routing
  to operator review. Phase I-D below answers the question with data.

### 5. Performance implications

Negligible: a few extra JSON fields per debate turn, one more indexed
field. The real cost is consensus passes on attribution quality
(~N× adapter calls per definition, same trade as hierarchy consensus).

### 6. Migration strategy

Solved problem in this codebase:

- Optional pydantic fields default cleanly on legacy documents — no
  migration required to deploy the schema.
- `backfill-intents` follows `backfill-contexts` verbatim: dry-run by
  default, `--apply` writes, glossary refresh at the end.
- The 2026-06-10 nine-DB context-seeding sweep is the operational
  rehearsal for the same procedure on intents.

### 7. Risk: semantic overfitting

The serious risk. Intent attribution is *interpretation of the author*;
a small model will project genre conventions ("published theology book"
→ everything tagged `teach`/`persuade`). Mitigations:

- multi-pass unanimity before storing a tag;
- separate `intent_confidence`, low values routed to review, never
  silently stored;
- **§4 Neutrality (requirements NFR) tension**: intent/valence
  annotations must be framed as *source-deployment metadata* ("the
  corpus deploys this concept to X"), never as the term's semantics —
  otherwise bias is loaded into the internal language the NFR says to
  keep neutral. The S2.25 prose guidance already threads this needle;
  structured fields must keep the same stance.

### 8. Risk: ontology explosion

Controllable iff one rule is held: **intent never creates entries.**
Same meaning deployed with different intent = one entry; the intent is
recorded on the definition. Contradistinct splits remain justified by
*meaning* difference only (DQ-005 unchanged). If frames × intents were
allowed to multiply entries, a 100-term corpus becomes a 1,200-entry
mud-tree. This is draft ADR-024 and should be accepted *before* any
intent code lands.

## Positions on the proposal's remaining items

- **Effectiveness confidence (perlocution):** defer, per the proposal's
  own recommendation. No observation channel exists; storing it now
  would be unfalsifiable decoration.
- **Reinforcement vs transition:** conceptually sound, no product hook
  in an ontology builder. Revisit if a conversational layer ever tracks
  state.
- **Multilingual via illocution:** right long-term frame; the schema is
  quietly ready (`DefinitionVersion.language` exists, currently always
  `"en"`). A translation is another DefinitionVersion on the same
  `(mpl_label, context_id)` with a different `language`. No new design
  needed when the time comes.
- **Hierarchical context (linguistic/cultural/relational/temporal/
  intentional):** don't build the hierarchy; the `kind` discriminator
  gives the first useful distinction (frame vs intent) and further
  kinds can be added when a real corpus demands them.
- **"When does contextual variation become a new concept?"** — the
  working answer the codebase embodies: new *frame* on the same entry
  while the referent is the same; new *entry* (contradistinct split)
  when the referents are distinct. Worth recording as the DQ-005
  resolution when the operator confirms.
- **AI vs human authorship weighting:** provenance already
  distinguishes (`model_used`, `created_by="operator"`); a retrieval
  rank boost for operator-authored definitions is a one-line scorer
  change if ever wanted.

## Draft ADRs (pending operator acceptance)

These are drafts. On acceptance they move into
`docs/architecture-decisions.md` with final numbers; on rejection this
section records why they were not taken.

> **DRAFT ADR-024 — Intent annotates definitions; it never creates
> entries or enters labels.** Inferred illocution (intent tags,
> intentionality, intent confidence) is stored as optional metadata on
> `DefinitionVersion`. It never justifies a new ontology entry, never
> partitions an existing entry, and never appears in an MPL label
> (ADR-021). Contradistinct splits remain driven by meaning difference
> alone (DQ-005). Rationale: prevents multiplicative ontology growth
> (frames × intents) and keeps the §4 neutrality NFR intact — intent is
> deployment metadata about the source, not semantics of the term.

> **DRAFT ADR-025 — Intent attribution uses ordinal intentionality and
> a separate intent confidence, both consensus-gated.** `intentionality`
> is `low | medium | high` (never a float score); `intent_confidence`
> is on the standard 0–10 scale and is distinct from definitional
> confidence and from any future effectiveness measure (which is
> perlocution and out of scope). Intent tags are stored only on
> multi-pass unanimity (S2.5 machinery); below-threshold attributions
> route to operator review. Rationale: local models cannot support
> float-precision intent scores (S2.4 direction-variance evidence), and
> a wrong-but-stored intent is worse than an absent one.

> **DRAFT ADR-026 — Utterance-level semantic instances are out of
> Mahalath's scope.** The proposal's `semantic_instance` document
> (locution span + context + illocution per utterance) is
> passage/memory-shaped work and belongs to Tirzah if anywhere
> (ADR-014). Mahalath absorbs the concept-level residue only:
> per-definition intent annotation and (optionally) `source_quotes`
> locution anchoring on entries.

## Recommended phasing (if greenlit)

Sequenced **after retrieval S-C** — intent-aware retrieval composes
with `build_bundle`, so the bundle layer should exist first.

1. **I-A — schema + taxonomy.** `kind` on DefinitionContext;
   operator-seeded intent taxonomy; `intent_tags` / `intentionality` /
   `intent_confidence` on DefinitionVersion; accept ADR-024/025/026.
2. **I-B — ingestion.** Debate output contract extension + scoped
   pipeline backfill + `backfill-intents` CLI (dry-run default).
3. **I-C — surfaces.** Retrieval filter + `Meaning` fields + web UI
   badges + glossary export fields.
4. **I-D — evaluation gate.** A/B a corpus run and measure
   consensus-pass unanimity rate on intent tags. This answers "can
   intentionality be inferred reliably enough?" with data; if unanimity
   is low, intent storage stays operator-only and the model pathway is
   shelved without schema cost.

## Decision queue for the operator

Recorded as DQ-013 / DQ-014 in `docs/architecture-decisions.md`:

- **DQ-013:** adopt structured illocution (I-A..I-D) at all, and if so,
  after S-C as recommended?
- **DQ-014:** accept the I-D evaluation gate as the go/no-go criterion
  for *model-sourced* intent attribution (operator-authored tags are
  safe regardless)?
