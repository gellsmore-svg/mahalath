# Mahalath — review of 1.4.0 `detailed_text` and ADR-033, with operator decisions

**Date:** 2026-08-10 · **Reviewed:** `master` @ `ba547ba`, version **1.4.0**
**Revised:** 2026-08-10 after operator discussion — several findings below are
corrected or withdrawn, and four new requirements are recorded in Part 3.
**Actioned:** 2026-08-11. All findings and all four requirements are implemented;
see the Unreleased section of `CHANGELOG.md`. Status per item is marked inline.

**Scope:** two pieces of new work at different maturities.

| Item | State | Reviewed as |
|---|---|---|
| 1.4.0 `detailed_text` (`9cc8c35`, 09:20) | shipped code | functional + code review |
| ADR-033 / `docs/scholarly-layer.md` (`d8e8ae9`, 11:10) | design only | design review |

**Method:** the feature was run against an isolated copy of the live corpus with
a real model and probed adversarially; the design was read against the code it
will land on. Claims are backed by a run or a query, reproduced inline.

**Baseline:** `490 passed` in 190 s (up from 481 at 1.2.0; 7 new tests in
`tests/test_detailed.py`). `ruff check src tests` clean.

---

## Part 1 — 1.4.0 `detailed_text`

### What the feature is

Every ontology entry carries a short definition per frame — the sentence two
agents argued their way to, and the authoritative statement of what the entry
means. 1.4.0 adds an optional longer version alongside it: two to four
paragraphs restating the *same* sense in fuller prose, for someone reading the
glossary rather than a machine consuming the lexicon. It is generated
automatically when a definition is written, and surfaces in retrieval, the
glossary export, chat and the web UI.

### What works

- **The invariant is enforced, not just asserted.** `detailed_text` is a field on
  the existing `DefinitionVersion`, and the prompt opens by forbidding a
  different meaning, a second frame, or any contradiction of the short
  definition. That is the correct shape: a rendering of one sense, not a new axis
  of polysemy.
- **It composes correctly with the 1.3.0 no-op fix.** The redefine path returns
  early on a no-op (`staleness.py:1315`) before reaching the enrichment call at
  line 1362, so the 44% of redefinitions that change nothing do not now buy an
  extra model call.
- **Indexing is right on both paths.** Accept uses `definition_index=0`, which is
  safe because `_write_accepted` inserts a new entry with exactly one definition;
  redefine uses `-1` because it appends.
- **Failure cannot break a write.** Both hooks are wrapped so an expansion error
  cannot fail an accept or a redefine.
- **Output quality is faithful.** Generated live against an isolated copy of the
  corpus (`gemma2:2b`) for `MPL-001`: same sense, no competing definition, no
  contradiction. Timing was 3.5 s per definition on that small model.

### Findings

The operator's test for this feature is: **a definition written from now on must
come out complete — short text, exposition, provenance and conversation record —
so that nothing ever needs filling in afterwards.** The findings below are ordered
against that test.

#### D1 (critical) · The redefine path generates an exposition with no source text — **FIXED**

The prompt instructs the model to "note how the term is used in this corpus when
relevant". The accept path passes a source snippet (`ontology.py:234`, `:455`).
**The redefine path does not** — the hook in `staleness.py:1362` calls
`enrich_definition_with_detail` with no `source_snippet`.

So a definition rewritten by the nightly pass gets an exposition asked to
describe corpus usage with no corpus in front of it, and the model supplies its
own. Observed in a live run:

> This framework is used throughout the corpus when discussing the ontology of
> reality. For instance, we might say *"the Relational Substrate of this system
> contains a specific kind of dynamic interplay that governs causality."*

Hedged, so not a fabricated citation — but presented as corpus usage and derived
from nothing in the corpus. This is a live write path producing incomplete work.

**Fix:** pass a snippet from the entry's source document on the redefine path, or
drop the corpus-usage instruction from the prompt when no snippet is supplied.

#### D2 (critical) · No conversation record is written for the exposition — **FIXED**

`detailed.py` calls the adapter and stores the returned prose. It writes **no
`decision_log` row and no `agent_exchanges` row**, and the definition carries no
link to one. There is also no record of which model produced it: `model_used` and
`created_at` on the definition describe the original debate. I generated an
exposition with `--model gemma2:2b`, overriding the configured model, and nothing
recorded that.

This is the direct blocker on the operator requirement in Part 3.1 — the prompt
and response that produced an exposition are gone the moment the call returns, so
there is nothing to surface later. Every exposition generated between now and the
fix is history that cannot be recovered.

**Fix:** write a decision-log row (and exchange) for the expansion call, link it
from the definition, and record the model and timestamp of the expansion
distinctly from the debate's.

#### D3 (low) · `--max-items` counts successes, not attempts — **FIXED**

`backfill_detailed_definitions` stops on `result.written >= max_items`, so the
cap holds while generation succeeds and stops holding when it fails. Measured on
a 20-entry probe with `max_items=3`: 3 adapter calls when healthy, **20** when
every generation failed. Reproduced through the CLI — asked for 2, attempted 5.

Now scoped as a **dev-tool annoyance**, not a production risk (see Part 3.4).
Worth knowing that the other backfills in this repo already do it correctly by
slicing the work list up front — `backfill-intents` and `backfill-contexts` both
use `[:max_items]`, and `intents` tracks an explicit `attempted` counter. This is
one new file that diverged from the established pattern, not a pattern to
rethink.

#### D4 (low) · The failure reason is dropped from the machine-readable result — **FIXED**

With Ollama unreachable, the adapter produced *"could not connect to
http://192.168.0.1:11434/api/generate ([Errno 111] Connection refused). Is the
daemon reachable from here?"* — host, reason and remediation. That went to the
log. The JSON the command prints says `"generation failed"`.

### Withdrawn

- **A finding that the backfill path generates without source text.** Correct as
  an observation, but the backfill is a development convenience against a corpus
  that gets rebuilt; it is not a production path and the operator does not need
  the existing 188 definitions filled. The equivalent gap on the *redefine* path
  is real and is retained as D1.
- **A finding that 1.4.0 "already violates" ADR-033's decision-log rule.**
  Wrong framing. The two landed on the same day — 1.4.0 at 09:20, ADR-033 at
  11:10 — so the rule is a forward-looking commitment written after the code, not
  a requirement that was missed. Nothing regressed. The accurate statement is D2:
  the exposition path carries no conversation record, and ADR-033 says future
  write paths should.

---

## Part 2 — ADR-033 (design)

The document is well built: the layering is stated as an invariant, the
same-document scope is argued from existing ADRs rather than asserted, the
non-goals are explicit, and the acceptance criteria are testable. Two halves are
worth separating, because the operator wants one of them and not the other.

### 2.1 Transcript productization — wanted, and the priority — **SHIPPED as ADR-034**

ADR-033 §Transcripts proposes surfacing the debate record: a
`mahalath show-decision <id>` command and a "Show debate" control on the entry
page. This is what the operator asked for. Current state, verified:

| Layer | Conversation captured? | Readable? |
|---|---|---|
| Short debated `text` | **Yes** — `decision_log` + `agent_exchanges`, every prompt, response and confidence score | **No** — no CLI command exists; the web page prints the `decision_log_id` as a bare uneditable string. Reading it means querying MongoDB directly. |
| `detailed_text` | **No** — nothing is written (D2) | Nothing to read |
| `scholarly_text` | Does not exist | — |

So the requirement fails at three different depths. The debate half needs
surfacing only. The exposition half needs capture adding before anything can
surface it, which makes D2 the more urgent of the two.

### 2.2 Lesson memory — not what was asked for; recommend deferring — **DEFERRED to DQ-015**

The other half of ADR-033 proposes that after each accepted definition a model
reads back over the transcript and distils it into a **lesson card** — a short
structured summary of what worked, what failed, what must not be repeated, and
which distinctions proved useful. Those cards are then injected into the prompts
of later debates on the same document, so the system starts from what was already
worked out rather than rediscovering it.

This is a different feature from transcript visibility and was not what the
operator was asking for. Assessment, as requested:

**In favour.** The corpus came from three documents and one produced 100 of the
119 entries, so there is genuinely a lot of repeated reasoning over the same
material within a single document. The scope constraint is narrower in principle
than it turns out to be in practice.

**Against, for now.** A lesson card is a model-written instruction that shapes
every subsequent debate on that document, and it would be the only model output
in Mahalath that does not pass through the operator — proposals queue for accept
or reject, intent tags require unanimity across passes, definitions must survive
a debate. Its failure mode is silent: a wrong `do_not_repeat` steers later
definitions with nothing to catch it.

**Recommendation: build 2.1 first and revisit.** The lessons cannot be evaluated
until the transcripts they are distilled from can be read, so the wanted feature
is a prerequisite for judging the unwanted one. Read a few real debates first and
the question answers itself with evidence.

### 2.3 The active document is not currently derivable — **FIXED**

ADR-033's central guarantee is that lesson and prior-thinking retrieval never
crosses `source_document_id`. Its acceptance test is "zero cross-document hits".
But the code cannot yet identify which document triggered a given write — both
audit paths take the **first** entry in the list:

```
staleness.py:1003   entry.source_document_ids[0] if entry.source_document_ids else ""
ontology.py:387     … or (entry.source_document_ids[0] if entry.source_document_ids else "")
```

**33 of 119 entries already carry more than one source document**, so this is not
hypothetical. A redefine triggered by document B on an entry first evidenced by
document A records A. Any scoping built on that will filter correctly and still
retrieve the wrong material. Threading the triggering document id through the
debate and redefine context belongs ahead of everything else in the build order.

### 2.4 Smaller design notes

- **`epistemic_status`** (`solid | contested | provisional`) is a new governed
  ordinal. Ship the validator with the vocabulary — the equivalent constants in
  Keturah shipped without one and nothing enforces them.
- **Budget criterion.** The acceptance criteria say the bundle must never drop a
  frame or the codified sense. Add explicitly that `detailed_text` and
  `scholarly_text` are both droppable *before* any frame.
- **Lesson storage lean is right.** A dedicated collection with a mandatory
  `source_document_id` index makes the constraint enforceable at the query layer
  rather than in prompt-assembly code, which is what the "zero cross-document
  hits" test needs in order to mean anything.

---

## Part 3 — Operator requirements (2026-08-10)

Four requirements recorded from the review discussion. These are decisions, not
findings.

### 3.1 Conversation history must be viewable for every prose layer

Every model call that contributes prose to a term — the debate, the exposition,
and the scholarly note when it exists — writes a conversation record; that record
is linked from the definition; and there is a way to open it from the CLI and
from the entry page in the web UI. The purpose is being able to understand after
the fact how a given result was arrived at.

Current state is §2.1 above. D2 is the blocking piece.

### 3.2 New definitions must never require a backfill

A definition written from now on comes out complete at write time. Backfill
commands remain acceptable as development conveniences against a corpus that is
rebuilt anyway, but no production write path may leave a field for a later sweep
to fill. D1 and D2 are both breaches of this test.

### 3.3 Related-document term traceability

**Not deduplication.** A new document is processed in full and the original
document's terms are left untouched. What is added is a recorded relationship, so
that new terms arising from a related document are traceable to the older terms
they correspond to. The purpose is comparison: run a different model or a changed
process over related material and see how the resulting term set differs from the
one already held.

Requirements as stated:

- Relatedness between documents is judged by **the LLM** — asked whether an
  incoming document is related to anything already processed — not by a
  mechanical text diff.
- A hash forms part of the provenance record.
- The link is recorded by joining the two document source records, and then
  matching terms across them.

Current state: ingestion takes a SHA-256 of the whole file and skips a
byte-identical re-drop (`ingestion.py:101-104`). That is the entire duplicate
story — a revised edition, a reformatted export or a reused chapter is treated as
wholly new, and its terms carry no relationship to the originals. The similarity
machinery that exists (`embeddings.py`, `mappings.py`) matches terms and
definitions, not documents.

This is new ingestion capability rather than a field, and it is the first thing
in the estate that would let the question "did that change improve the output?"
be answered. It also interacts with §2.3 — where two documents share a passage,
"the same source document" needs a defensible answer.

### 3.4 Operator review is only for terms still below threshold after recursion

The operator should be asked to review a term only when the system has finished
trying and confidence is still short. Everything that resolves on re-debate
should never reach a person.

**Current state.** The mechanism is half there and the interface is passive.

- A debate accepts when `min_confidence >= runtime.confidence_threshold`
  (`debate.py:168`), default **8.0** on a 0–10 scale.
- Below that the term goes to the undecided queue with a `reason`
  (`below_threshold`, `iteration_cap`, `conflict`, `moderator_block`,
  `proposed_term`), a `last_confidence` and an `escalation_level`.
- Nightly REM re-debates queued items; `escalation_level` increments.
- `/undecided` in the web UI lists everything pending — term, reason, last
  confidence, escalation, decision-log id, created date.

So the data needed to make this decision is already recorded. Two gaps: the page
shows **everything pending**, including items the system has not finished
retrying, and it has **no actions** — no accept, no reject, no override.
Checking the history, no accept/reject on undecided items has ever existed and
nothing was deleted; `/undecided` has been a read-only list since it was
introduced. It looks like a review interface without being one.

**Recommended threshold (reviewer's call, as asked).** Keep one confidence number
rather than introducing a second — `confidence_threshold` stays at **8.0** — and
gate the operator's attention on *attempts*, not on a different score:

| Condition | Behaviour |
|---|---|
| `min_confidence >= 8.0` | Auto-accept. Unchanged. |
| `below_threshold`, `escalation_level < 2` | Re-debate overnight. Not shown to the operator. |
| `below_threshold`, `escalation_level >= 2` and still `< 8.0` | **Surface for review.** Three attempts have been made; further recursion is not going to resolve it. |
| `conflict` or `moderator_block`, any escalation | **Surface immediately.** These are structural disagreements — whether a term holds one meaning or two — and re-debate does not resolve them; that judgement is the operator's. |
| `proposed_term` with no debate yet | Never surfaced until it has been debated at least once. |

Two attempts after the first is the number because the re-debate is the same two
agents over the same source snippet; a third identical pass rarely moves a score
that has not moved twice. If the effectiveness report later shows items resolving
at escalation 2 or 3, raise it — the data to check that is already collected.

The `/undecided` page then becomes the review interface: filtered to the rows
above, with accept, reject and edit actions writing back to the same audit chain
the proposals queue already uses.

The live queue is currently empty (0 items), so this can be built and tested
without a backlog in the way.

---

## Suggested order

1. **D2** — capture and link a conversation record for the exposition. Blocks
   requirement 3.1, and every exposition generated before it is unrecoverable
   history.
2. **D1** — pass a source snippet on the redefine path. Breach of requirement
   3.2 on a live write path.
3. **§2.1 surfacing** — `show-decision` on the CLI and "Show debate" on the entry
   page, over the debate records that already exist.
4. **§3.4** — filter `/undecided` to the gate above and give it accept/reject
   actions.
5. **§2.3** — thread the triggering `source_document_id` through debate and
   redefine. Prerequisite for any ADR-033 implementation.
6. **§3.3** — LLM-judged document relatedness and cross-document term
   correspondence. The largest piece, and the one with the most leverage on
   evaluating future changes.
7. **D3, D4, §2.4** — dev-tool limit, error text, and the ADR's smaller points.

Lesson memory (§2.2) is deliberately absent from this list; revisit after step 3.

The 1.4.0 feature is well built and lands the invariant it set out to land. Its
two critical findings are both the same shape — a write path that produces prose
without the evidence or the record that should accompany it — and both are
directly in the way of what the operator actually wants from the transcripts.
