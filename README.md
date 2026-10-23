# Mahalath

**Mahalath turns a body of documents into a precise, auditable lexicon of
meanings, built by AI agents that debate each definition.** Drop Markdown
into `input/` and come back to a glossary with sharp definitions, a
parent/child hierarchy, several meanings per term where the corpus really
uses several, and full provenance. A retrieval API then lets another LLM
reason in those exact meanings rather than in ambiguous natural language.

It is local-first (MongoDB and Ollama). Frontier-model review through the
Anthropic API is optional.

## The idea

Natural language is ambiguous, and AI-to-AI reasoning suffers for it.
Mahalath gives every meaning an opaque, permanent label (`MPL-004`), one or
more debated definitions, a place in a hierarchy and a full audit trail.
Human words become approximate handles onto those meanings. One term can
hold several co-equal meanings, each tied to the *frame* it speaks within: a
`field` is one thing to a physicist, another to a farmer and another to a
database designer. A consuming LLM looks a term up, receives every codified
meaning with provenance, and cites the `(MPL label, frame)` pair it used.

A lexicon belongs to **one language**; the live one is English. Languages
are peers, never derived from each other. Cross-language relationships are
explicit, debated mapping assertions, never translation at ingestion.

## Is it for you?

**Good fit:** a corpus whose terms must be used precisely, such as a legal
code, an engineering handbook or a research literature, where you want
definitions that are argued, traceable and reviewable, and an ontology an
LLM can query.

**Not a fit:** a quick glossary of one document. Every term goes through
multi-agent debate on a local model, which takes time. It is also not a
translator: cross-language mappings relate meanings and do not translate
text.

## Try it

You need MongoDB running locally and Ollama with `gemma4:e2b` (debate)
pulled; `bge-m3` is only needed for cross-language mappings. Mahalath is
also on PyPI (`pip install "mahalath[web]"`).

```bash
# 1. Install
git clone https://github.com/gellsmore-svg/mahalath
cd mahalath
python -m venv .venv && .venv/bin/pip install -e ".[dev,web]"

# 2. Prerequisites: MongoDB running locally, and Ollama with the models
#    pulled (gemma4:e2b for debate; bge-m3 for cross-language mappings).
#      ollama pull gemma4:e2b
#      ollama pull bge-m3
.venv/bin/mahalath db-ping

# 3. Prepare a fresh database — creates every collection + index and
#    seeds the standard taxonomies. Idempotent; safe to re-run.
.venv/bin/mahalath init

# 4. Process a document
cp my-source.md input/
.venv/bin/mahalath process-input --max-terms 10

# 5. Browse the result
.venv/bin/mahalath list-ontology
.venv/bin/mahalath export-glossary --format md --out ontology/glossary.md

# 6. Query it like an LLM would
.venv/bin/mahalath retrieve "field" --format text --budget 800
.venv/bin/mahalath subtree MPL-001 --depth 2
.venv/bin/mahalath propose-term "lattice" --context "…source snippet…" --near MPL-004

# 7. See how a term was arrived at, and decide what the system couldn't
.venv/bin/mahalath show-decision MPL-001            # every conversation behind it
.venv/bin/mahalath show-decision MPL-001 --verbose  # including the prompts sent
.venv/bin/mahalath needs-review                     # only terms still stuck after retries
.venv/bin/mahalath accept-undecided <decision-log-id> --note "close enough"
```

## How it works

```
input/file.md  →  ingest + SHA-256 dedupe + archive
              →  heading-aware chunk (any document size)
              →  LLM-driven candidate term extraction
              →  multi-iteration debate (PrecisionCritic + SynthesisExplorer)
              →  optional external grounding (ADR-038), still on that debate:
                 corpus_only by default; hybrid or external when configured
              →  if accepted: ontology entry (frame-tagged definition; debate path
                 records a per-definition consensus score) + hierarchy review
                 pass (3-pass consensus)
              →  if undecided: queue for nightly REM re-debate
              →  reference extraction → reverse index → staleness cascade when
                 upstream entries change → audit/redefine self-healing
              →  glossary auto-export to ontology/glossary.{md,json}
```

Every model call is recorded with a `decision_log_id` and stays queryable;
every operator accept, reject or rollback writes to the same audit chain.
`mahalath show-decision MPL-001` replays the full conversation behind any
term.

## What you get

| Capability | In short |
|---|---|
| **Polysemy, first-class** | Definitions are tagged with a context frame; co-equal meanings never supersede each other |
| **Retrieval for LLMs** | Term search, full codified meanings, **token-budgeted bundles** that never drop a frame and always include every term a definition cites, subtree views and `propose-term`. Library, CLI and HTTP |
| **Review only what is stuck** | You are asked about a term only after the system has finished re-debating it (`needs-review`) |
| **Self-healing** | When an entry changes, every entry that cites it is flagged stale and re-audited overnight |
| **External grounding** | Optionally check a debated definition against outside sources; `corpus_only` by default |
| **Intent annotation** | *Why* the corpus uses a term (teach, warn, persuade…), admitted only when independent passes agree unanimously |
| **Self-analysis** | Calibration of agent confidence against your decisions, queue health and coverage (`mahalath effectiveness`) |
| **Style overlay** | Per-corpus voice notes that make definitions follow the source's framing; the biggest single quality lever in A/B testing |
| **Web UI and chat** | `mahalath serve`: browse, review and ask grounded questions with MPL citations |

Every capability, workflow and command in detail:
[docs/guide.md](docs/guide.md).

## Know before you rely on it

- **`frontier-review` sends content off your machine.** It sends the
  definitions under review and your style overlay to Anthropic's API (it
  needs `ANTHROPIC_API_KEY`). Everything else runs locally.
- **`consensus_score` is not comparable across paths.** Only the debate path
  records one. `null` or missing means "not a debate consensus", not zero.
- **External grounding is off by default**, and overnight rewrites stay
  inside the corpus unless `semantic_grounding.overnight` is enabled.
- **Design only, not built yet:** the scholarly layer and same-document
  lesson memory (ADR-033). Cross-language mappings work, but the live
  lexicon is English.

## Design commitments

- **Labels are immutable and opaque** — re-parenting moves tree edges, never the label; no semantics in the key (ADR-018/021).
- **Human labels are approximate interfaces, not the ontology** (ADR-019).
- **Languages are discrete peer lexicons** — a label addresses a meaning within one language's lexicon; cross-language equivalence is only ever an explicit, debated mapping assertion, and locale is metadata, never structure (ADR-028/029/030).
- **Retrieval surfaces all frames; the caller disambiguates** (ADR-022). **Returned meanings are reference-closed** (ADR-023).
- **Intent annotates definitions; it never creates entries or enters labels** (ADR-024).
- **Everything is auditable** — every accepted definition links to its debate transcript; every structural change is a proposal with a rollback path.

- **Self-analysis is read-only and file-snapshotted** — the effectiveness layer aggregates the audit trails it reports on but can never write to them (ADR-027).

The decision record (ADRs and open questions) is in
[docs/architecture-decisions.md](docs/architecture-decisions.md), and the
retrieval design is in [docs/retrieval-spec.md](docs/retrieval-spec.md).

## Status

Stage 2, deep. The self-sustaining loop works end to end: ingest, debate,
persist, hierarchy review, operator and frontier queues, nightly re-review,
self-healing and glossary export. So do the retrieval layer, the intent
extension and nightly self-analysis, all validated against a live corpus.
The test suite includes live MongoDB round-trips; the history is in
[CHANGELOG.md](CHANGELOG.md).

[Guide](docs/guide.md) · [project brief](docs/project-brief.md) ·
[terminology](docs/terminology.md) · [okf/](okf/index.md) concept map ·
Apache 2.0, see [LICENSE](LICENSE)
