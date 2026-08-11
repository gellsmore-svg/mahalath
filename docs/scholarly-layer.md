# Scholarly layer + same-document reasoning memory

**Status:** design accepted (ADR-033) · **implementation not yet started**  
**Date:** 2026-08-10 · **Amended 2026-08-10 (operator, same day)**

> **Amendment summary.** Three changes to this document, recorded the same day it
> was written. See `docs/review-2026-08-10-detailed-and-scholarly.md` for the
> reasoning.
>
> 1. **Lesson memory is deferred** (§Lesson memory below). It was not the
>    capability being asked for, and it cannot be evaluated until transcripts are
>    readable. Moved to **DQ-015**. This document retains the design so it does
>    not have to be rewritten if it is later adopted.
> 2. **Transcript productization is widened and promoted to ADR-034.** It is the
>    capability that *was* wanted, and it now covers every prose layer — the
>    debated `text`, `detailed_text`, and scholarly — not just the debate. Note
>    that `detailed_text` currently captures **nothing**, so this is a capture
>    problem before it is a surfacing problem.
> 3. **Active-document threading is step 0**, ahead of everything in the build
>    order below. The "same source document" rule cannot currently be enforced —
>    see §Prerequisite.
>
> Related new decisions from the same discussion: **ADR-035** (no backfill for
> new definitions), **ADR-036** (related-document term traceability),
> **ADR-037** (operator review gated on confidence after recursion).

**Related:** ADR-015 (source preserved), ADR-019 (MPL is identity), ADR-022
(all frames), ADR-023 (reference closure), ADR-024–026 (intent ≠ semantics),
1.4.0 `detailed_text` (pedagogical exposition)

---

## Problem

Mahalath already produces a **precise, multi-agent-debated sense** for each
`(term, frame)` and (as of 1.4.0) an optional **pedagogical expansion** of that
same sense. Two needs remain:

1. **Scholarly depth** — a third prose product for careful / technical readers:
   corpus-situated, contrastive, epistemically honest — still the *same* sense,
   not a competing definition.
2. **Learning from past thinking** — debates already write `decision_log` +
   `agent_exchanges`, but that trail is mostly audit. Future debates and the
   scholarly pass should reuse **prior reasoning, mistakes, and distinctions**
   so the system compounds judgment instead of re-discovering the same errors.

### Hard scope constraint (operator, 2026-08-10)

**Past thinking is scoped to the same source document**, not to other corpora
or other ingested documents.

When a definition for term *T* is being debated or elaborated, retrieved
lessons and prior debate material may only come from reasoning that was
produced while processing **the same `source_document_id`** (or the same
document’s re-debate / redefine chain that remains anchored to that document).
Cross-document or cross-corpus “memory of all debates ever” is **out of scope**
for this design.

Rationale:

- Mahalath’s definitions are **document-evidenced** (ADR-015, ingestion →
  debate from document context). Contamination from another book’s framing
  would reintroduce the ambiguity the lexicon is meant to remove.
- Intent (ADR-024) is already *source-deployment* metadata, not global
  semantics. Lesson memory follows the same grain: **this source’s argument
  history**, not estate-wide folk wisdom.
- Cross-language mappings (ADR-029) remain a separate, explicit pathway; they
  are not a backdoor for “lessons from other lexicons.”

---

## Three prose layers, one sense

Each `DefinitionVersion` addresses **one meaning in one frame**. Prose layers
are **renderings of that meaning**, not new axes of polysemy.

| Layer | Field (proposed / existing) | Role | Authority |
|-------|----------------------------|------|-----------|
| **Codified** | `text` (existing) | Short, debate-tested sense; identity, references, embeddings | **Authoritative** for what the entry *means* |
| **Pedagogical** | `detailed_text` (1.4.0) | Clear 2–4 paragraph exposition for general readers | Subordinate; must not contradict `text` |
| **Scholarly** | `scholarly_text` (+ optional structure) | Analytic note: corpus situation, contrasts, open questions, epistemic status | Subordinate; must not invent a new frame |

```
Frame: structural
├── text              ← debate product (PrecisionCritic × SynthesisExplorer)
├── detailed_text     ← same sense, pedagogical (single-model, best-effort)
├── scholarly_text    ← same sense, scholarly (uses transcript + same-doc lessons)
└── decision_log_id   ← full conversation trail (messages + agent_exchanges)
```

**Invariant:** if scholarly prose would require a different sense, that is a
**new definition / new debate** (possibly a new frame), not a field on this
version.

---

## Transcripts (already largely present)

### What is stored today

- `DefinitionVersion.decision_log_id` → `decision_log` row  
  (`messages[]`: role, content, confidence, model, iteration)
- `agent_exchanges` rows for the same id  
  (full prompt + response per turn)

Debate path is strong. REM redefine now links a decision log; exchange richness
should match debate over time.

### Productization (implementation)

- Every definition write path ensures a non-null `decision_log_id` when a model
  was involved.
- Operator surfaces: `mahalath show-decision <id>`, web “Show debate” on an
  entry definition.
- Transcripts remain the **ground truth**; they are not replaced by lessons.

---

## Lesson memory (same document only)

> **DEFERRED — DQ-015 (2026-08-10).** Everything in this section is design that
> is *not* being built now. It was not the capability requested, and its quality
> cannot be judged until the transcripts it distils are readable (ADR-034).
> Retained verbatim so it does not need rewriting if adopted later. The open
> sub-question if it is: a lesson card would be the only model output in Mahalath
> that does not pass an operator gate, and its failure mode is silent — a wrong
> `do_not_repeat` steers every later debate on that document with nothing to
> catch it.

Audit answers “what was said?”. Lessons answer “what should the next pass on
*this document* do differently?”

### Lesson card (distilled, structured)

Produced after accept (or nightly for missing cards), **keyed by document**:

```text
source_document_id: <required>
decision_log_id:    <required>
mpl_label / term / context_id (when known)
what_worked: [...]
what_failed: [...]          # e.g. conflated substrate with runtime order
do_not_repeat: [...]        # critic objections that stuck
useful_distinctions: [...]
created_at / model_used
```

Storage options (implementation choice):

- subdocument on `decision_log`, or
- collection `definition_lessons` with mandatory `source_document_id` index

### Retrieval rule (normative)

When preparing a debate, redefine, detailed, or scholarly generation for a
candidate or entry:

1. Resolve the **active document** = the `source_document_id` of the current
   debate context (or the entry’s primary / triggering source document for
   that write).
2. Load lessons (and optional prior `decision_log` summaries) **only where**
   `source_document_id` equals that document.
3. Prefer lessons for: same term, aliases on this document’s entries, parent /
   child labels **of entries also evidenced by this document**, frames already
   used in this document’s ontology slice.
4. **Never** pull lessons whose `source_document_id` differs, even if the
   canonical term string matches.

Within a single document, multiple terms and re-debates form a **local
reasoning graph**. Across documents, silence is correct.

### Injection into debate prompts

A short, bounded block (token-capped):

```text
## Prior reasoning on this source document (do not copy blindly)
- [MPL-004 structural] Critic rejected equating substrate with "background"…
- [MPL-001] Explorer over-claimed; critic forced a frame split…
```

Reputation-blind: lessons are about **arguments**, not “model X is better.”

---

## Scholarly generation

### Inputs (ordered)

1. Accepted `text` (must not contradict)  
2. `detailed_text` if present  
3. Frame name + description  
4. Source snippet(s) from **this** document  
5. This definition’s debate transcript (`decision_log` + exchanges)  
6. Same-document lesson cards (top-k, budgeted)  
7. Style overlay (corpus voice)

### Outputs

Minimum:

- `scholarly_text: str`

Recommended structure (store as nested object or parallel fields):

```text
scholarly:
  text: "..."
  open_questions: [...]
  rejected_senses: [...]      # from this debate / same-doc history
  near_misses: [...]
  corpus_anchors: [...]       # quotes from this document only
  epistemic_status: solid | contested | provisional
  derived_from_decision_log_ids: [...]
  model_used / created_at
```

### When it runs

| Trigger | Behaviour |
|---------|-----------|
| After debate accept | Enqueue scholarly job (prefer async / Hoglah); do not block accept |
| After REM redefine | Regenerate for the new definition version; retain history |
| CLI | `backfill-scholarly`, regenerate per label |
| Nightly REM | Fill missing scholarly for this DB’s entries |

Config knobs (proposed): `generate_scholarly_definitions: bool`, optional
heavier model for scholarly than for detailed.

### Retrieval / export

| Consumer | Depth |
|----------|--------|
| Agent-to-agent default bundle | `text` (+ frames); drop scholarly first under budget |
| Human glossary / deep explain | include scholarly |
| Budget invariant (ADR-023 spirit) | never drop a frame or codified `text` for scholarly bulk |
| Embeddings | continue to use `text` (identity); optional later scholarly embedding for human search only |

---

## Data flow

```
  Document D ingested
       │
       ▼
  Debate (PC × SE)  ◄──── lessons WHERE source_document_id = D
       │
       ├─► text                    (codified sense)
       ├─► decision_log + exchanges (full transcript)
       ├─► lesson card             (distilled; tagged with D)
       ├─► detailed_text           (pedagogical; optional)
       └─► scholarly_text          (scholarly; uses transcript + D's lessons)
```

---

## Non-goals

- Cross-corpus or cross-document lesson retrieval  
- Scholarly prose as a second competing sense  
- Replacing `decision_log` with summaries only  
- Blending scholarly quality into `consensus_score`  
- Tirzah memory of other books as automatic scholarly input (ADR-031 remains
  separate and opt-in for **context collation**, not lesson memory)

---

## Prerequisite — the active document is not currently derivable

**This blocks the hard scope constraint and must be done first.**

The rule above is that lesson and prior-thinking retrieval never crosses
`source_document_id`, with an acceptance test of *zero* cross-document hits. The
code cannot express that today: both audit paths take the **first** document in
the entry's list rather than the one that triggered the write.

```
staleness.py:1003   entry.source_document_ids[0] if entry.source_document_ids else ""
ontology.py:387     … or (entry.source_document_ids[0] if entry.source_document_ids else "")
```

**33 of 119 live entries already carry more than one source document.** A
redefine triggered by document B on an entry first evidenced by document A
records A. Any scoping built on that will filter correctly and still retrieve the
wrong material — the tests would pass and the data would be wrong.

Thread the triggering `source_document_id` through the debate and redefine
context before building anything below. ADR-036 (related-document linking) also
depends on a defensible answer here, since two related documents may share a
passage.

## Implementation order

0. **Active-document threading** — prerequisite above (ADR-033 amendment)
1. **Transcript capture + productization** — for **every** prose layer, not just
   the debate: `detailed_text` currently writes no record at all, so capture
   comes before surfacing. Then linkage and an operator "show debate" on the CLI
   and the entry page. (**ADR-034**)
2. ~~Same-document lesson distillation + injection~~ — **deferred, DQ-015**
3. **`scholarly_text` (+ structure)** generator — writes its own conversation
   record from the start (ADR-034), and completes at write time (ADR-035)
4. **Retrieval / glossary depth** (`short | detailed | scholarly`)
5. ~~Backfill over live corpus~~ — **not required.** Per ADR-035 the corpus is a
   development corpus that is rebuilt; backfill is a convenience, not a
   deliverable. What matters is that new definitions never need one.

`detailed_text` (1.4.0) stays as-is; scholarly does not subsume it.

---

## Open implementation choices

| Topic | Lean |
|-------|------|
| Lesson storage | Dedicated collection with `source_document_id` index |
| Async scholarly | Prefer Hoglah when `model_adapter=hoglah`; else best-effort sync with timeout |
| Multi-source entries | Entry may list several `source_document_ids`; active document for a write is the one in the current debate/redefine context, not the union of all sources |
| Undecided re-debate | Lessons from earlier undecided attempts on **same document + same term** are in-scope |

---

## Acceptance criteria (when built)

- [ ] Scholarly never written without an accepted `text` for that definition version
- [ ] **Every prose layer writes a conversation record with its own model and
      timestamp, linked from the definition, openable from CLI and web** (ADR-034)
- [ ] **No prose layer leaves a field for a later sweep to fill** (ADR-035)
- [ ] **A redefine-generated exposition receives a source snippet** — the current
      gap at `staleness.py:1362` (ADR-035)
- [ ] Active-document threading: a write records the **triggering**
      `source_document_id`, not `source_document_ids[0]`
- [ ] Bundle budget still never drops a frame or codified sense — and
      `detailed_text` / `scholarly_text` are both droppable **before** any frame
- [ ] `epistemic_status` is validated against its vocabulary on write
- [ ] Glossary/export can omit raw transcripts while keeping scholarly prose

Deferred with DQ-015 (re-add if lesson memory is adopted):

- [ ] ~~Lesson retrieval unit tests prove **zero** cross-`source_document_id` hits~~
- [ ] ~~Debate prompt fixtures include same-doc lessons and exclude other-doc lessons~~
