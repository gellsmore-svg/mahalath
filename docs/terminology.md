# Mahalath Terminology

Plain-language definitions of the words used across Mahalath's code,
commits, design docs, and working conversations. The aim is that no
term in this project should require context you don't have.

**This is not the same as the ontology glossary.** `export-glossary`
produces a glossary of the *corpus's* terms (what "Resonance" means in
the source material). *This* file defines Mahalath's *own* working
vocabulary (what "debate" or "mapping" means when we talk about the
software).

A **⚙︎ coined** marker means it's a word Claude introduced as shorthand
rather than a standard term — useful, but invented here, so always worth
spelling out.

---

## The core objects

- **Lexicon** — one body of entries in a single language. There is an
  English lexicon and a German lexicon; they live in the same database
  but never mix (an English entry and a German entry are separate things
  even when they mean the same idea).
- **Entry** (or **ontology entry**) — one concept Mahalath has defined.
  Each has an identifier, a canonical term, one or more definitions, a
  place in the hierarchy, and a confidence score.
- **MPL label** — the identifier for an entry, like `MPL-117`. It is
  deliberately opaque: the number tells you nothing about the meaning,
  the language, or where it sits in the tree. It never changes once
  assigned. The label, not the human word, is the real address of a
  concept.
- **Canonical term** — the human-readable name of an entry ("Kopplung").
  A convenience for reading and dictation, not the thing itself.
- **Definition** (or **definition version**) — the actual text saying
  what an entry means. An entry can accumulate several versions over
  time; the most recent is the active one, and older ones are kept as
  history. Each carries which model wrote it and its agreement score.
- **Frame** — the sense or aspect a definition speaks within. A word can
  carry more than one meaning (polysemy); each meaning is a definition in
  a different frame. (In the code this is a "definition context" of kind
  `frame`.)
- **Hierarchy / tree** — entries can have a parent (a broader concept),
  forming a navigable tree alongside the flat list.

## The debate (how a definition gets made)

- **Debate** — the process of arriving at a definition by having two
  models argue about it across a few rounds until they agree (or give
  up). The standard loop for defining a term.
- **PrecisionCritic** and **SynthesisExplorer** — the two roles in a
  debate. The Critic pushes for sharpness and finds fault; the Explorer
  proposes and broadens. They're currently run on *different* model
  families (mistral as Critic, gemma as Explorer) so agreement between
  them means two different models agree, not one model agreeing with
  itself.
- **Confidence** — a 0–10 score for how settled a definition is. A
  debate's confidence is the *lower* of the two roles' scores (the
  cautious one wins).
- **Threshold** — the score a debate must reach to be accepted (8.0). 
  Below it, the term doesn't get a confident definition and goes to the
  undecided queue.
- **consensus_score** — the agreement score captured for a *specific*
  definition at the moment it was accepted. Unlike the entry's overall
  confidence (which can drift later), this is a fixed snapshot.
- **Consensus roster / consensus passes** — a separate check that runs
  certain decisions (where an entry sits in the tree; what its intent
  is) across *three* model families in turn, so agreement means three
  different models independently concur. This is distinct from the
  two-role debate above — the third model (qwen) only joins these
  verification passes, never the definition debate itself.
- **Style overlay** — a short prompt describing the corpus's voice and
  conventions, fed into the debate so definitions match the source
  material's register and language (e.g. the German pilot has its own
  overlay so definitions come out in German).

## Confidence, decisions, and provenance

- **Undecided queue** — where terms wait when a debate couldn't reach the
  threshold. They get re-examined later rather than guessed at.
- **Proposal** (or **action proposal**) — a structural change (e.g.
  re-parenting an entry) queued for the operator to accept or reject,
  rather than applied automatically.
- **decided_via** — a tag recording *who* made a final call: the model
  consensus, the operator personally, or Claude acting as the operator's
  delegate (`operator_delegate`). Delegate decisions are kept separate so
  they don't pollute the measurement of how well the *models'* confidence
  predicts the *operator's* agreement.
- **Effectiveness / calibration** — Mahalath's self-analysis of its own
  decision quality: does the agents' stated confidence actually predict
  whether the operator agrees? Accrues nightly.

## Self-healing (fixing entries after the fact)

- **Staleness / stale** — an entry is "stale" when something it depends on
  has changed, so its definition might no longer hold. Stale entries are
  flagged for re-review.
- **Cascade** — staleness spreads: if an entry goes stale, entries that
  depend on *it* are flagged too.
- **Audit** — asking a model whether a stale entry's definition still
  holds against the current state of what it references.
- **Redefine** — rewriting a definition that an audit found out of date.
- **Re-debate** ⚙︎ — running a fresh debate on an *already-accepted* entry
  to improve its definition (e.g. upgrading the German entries from
  single-model to cross-family). New as of S2.50; distinct from redefine
  (which is about staleness, not quality).
- **REM** — a background "deep sleep" pass. Today it re-examines the
  undecided queue and takes nightly self-analysis snapshots. (Named for
  REM sleep; the idea is offline consolidation while no one's ingesting.)
- **Frontier review** — a model acting as adjudicator over entries
  waiting in the review queue.
- **Known-term guard** — when re-ingesting a document, terms that already
  exist aren't debated again; they just get recorded as also appearing in
  the new source. Stops duplicates.

## Cross-language mappings (M-C)

- **Mapping** — a typed statement that two entries *in different
  languages* relate in meaning. Not a translation — a judgment about how
  two meanings relate.
- **Relationship taxonomy** — the allowed kinds of mapping: `equivalent`,
  `partial_overlap`, `narrower_than`, `broader_than`. An operator-owned
  list, like frames and intents.
- **Shortlist** ⚙︎ (the code calls it **candidate scouting**) — the first
  step of making mappings: out of all entries in the other language, pick
  the few worth comparing to a given entry, so the expensive judging runs
  on a handful of pairs, not all of them. As of S2.51 this can be done by
  **meaning-closeness** (fingerprints) instead of a model picking from a
  list — the fix for the recall problem (it used to miss obvious matches).
- **Backfill embeddings** — the one-off step that computes and stores a
  fingerprint for every entry, so the shortlist has something to compare.
  Only this step calls the embedding model; making mappings afterwards
  just reads the stored numbers. Command: `backfill-embeddings`.
- **init / bootstrap** — `mahalath init` prepares a fresh database:
  creates every collection and index and seeds the standard taxonomies
  (intents, mapping relationships). Idempotent — safe to re-run.
- **Judging step** ⚙︎ (the code calls it **the gate**) — the second step:
  the models judge each shortlisted pair and the *majority verdict*
  decides accepted / rejected / unresolved.
- **Illocution comparison** — when two entries are mapped, a side-by-side
  of how each language *uses* the term (its intent profile — teaching,
  warning, etc.). Divergence in use is itself a translation-risk signal.
- **Resolution** — the operator (or Claude as delegate) settling an
  `unresolved` mapping by hand, recorded with provenance and preserving
  the models' original votes.

## Retrieval (the read side)

- **Retrieval** — the typed, read-only way another program (or a model)
  queries the ontology for entries and prompt-ready context.
- **Bundle** — a packaged, size-limited set of entries returned by
  retrieval — pruned to a token budget rather than dumping the whole
  tree.

## Models and infrastructure

- **Adapter** — the seam through which Mahalath calls a model, so the
  model stack can change without rewriting everything. The live one
  drives the local Ollama install.
- **Ollama** — the local model runtime (runs on the Windows side; reached
  over the command line from WSL).
- **Quantized model** — a model compressed to run on limited hardware
  (here, 8-bit). Cheaper to run, but with some loss of reasoning quality —
  the root of most of the quality issues we discuss.
- **Fingerprint** ⚙︎ (standard term: **embedding**) — turning a
  definition's *meaning* into a fixed list of numbers, arranged so that
  two definitions about similar ideas get similar number-lists. Lets the
  software compare meanings directly — including across languages. Planned
  as the fix for the shortlist recall problem.

## Sibling projects

- **Tirzah** — a separate memory-retrieval project. Shares Mahalath's
  philosophy but is kept runtime-independent (ADR-014): Mahalath does not
  depend on Tirzah to run.
  Semantic similarity / context-optimisation work was originally slated
  to live here. (If you see "TSR" anywhere, that was a dictation garble
  for Tirzah.)
