# Mahalath

Self-sustaining multi-agent ontology builder. Drop Markdown documents into `input/`, walk away, and come back to a definitionally-sharp glossary with parent/child relationships, polysemy-aware definitions, full provenance, and operator-reviewable proposal queues — plus a retrieval API that lets another LLM reason in the ontology's precise internal terms instead of ambiguous English.

The system is **local-first** (MongoDB + Ollama by default) with optional **frontier-LLM review** (Anthropic Claude API) for queue items the local model isn't confident about.

## The idea

Natural language is ambiguous; AI-to-AI reasoning suffers for it. Mahalath ingests a corpus and continuously refines a precise internal language (**MPL**): every concept gets an opaque, immutable label (`MPL-004`), one or more debated definitions, a place in a hierarchy, and a full audit trail. Human words (`substrate`) are treated as approximate interfaces onto that machine-native concept space — a single term can hold several co-equal meanings, each keyed by the *frame* it speaks within (structural, theological, physical, …). A consuming LLM retrieves by human term, receives every codified meaning with provenance, and cites the `(MPL label, frame)` pair it kept.

## What it does

```
input/file.md  →  ingest + SHA-256 dedupe + archive
              →  heading-aware chunk (any document size)
              →  LLM-driven candidate term extraction
              →  multi-iteration debate (PrecisionCritic + SynthesisExplorer)
              →  if accepted: ontology entry (frame-tagged definition, per-definition
                 consensus score) + hierarchy review pass (3-pass consensus)
              →  if undecided: queue for nightly REM re-debate
              →  reference extraction → reverse index → staleness cascade when
                 upstream entries change → audit/redefine self-healing
              →  glossary auto-export to ontology/glossary.{md,json}
```

Every model interaction is recorded with a `decision_log_id` and queryable forever. Every operator action (accept, reject, rollback) writes back to the same audit chain.

### Polysemy as a first-class citizen

Definitions are tagged with a **context frame** (a governed taxonomy of `DefinitionContext` rows). One entry can legitimately carry a structural definition *and* a theological one — they are co-equal; nothing supersedes anything. The web UI, chat, glossary export, and retrieval layer all group and label definitions by frame.

### Self-healing

Each entry records which other MPL labels its definitions mention (explicit + semantic matching, maintained incrementally on insert). When an upstream entry changes — redefined, re-parented, rolled back — every dependent is flagged stale, cascading with a cycle guard. Nightly REM jobs re-audit stale entries against current upstream state and either clear the flag or re-debate the definition.

### Retrieval layer (for an orchestrating LLM)

A typed read view over the ontology (`retrieval.py`), available as a library, CLI, and HTTP API:

- `search_terms` — resolve human terms to ranked matches (shared scorer + `$text` fuzzy index, branch/frame/status/confidence filters).
- `get_codified` — expand `MPL-004` (or the frame-scoped handle `MPL-004#structural`) into all meanings, tree path, references both directions, provenance, stale state.
- `build_bundle` — a **token-budgeted, prompt-ready bundle**: primary entries with *all* their frames (retrieval never collapses polysemy — the caller disambiguates), a mandatory **reference closure** (every codified term cited inside a returned description is included transitively, cycle-safe), ranked alternatives, and a compact NL rendering. Budget pressure trims breadth and verbosity in recorded steps; it never drops a frame or a closure node.
- `subtree` — limited-depth descendant summaries via the materialised ancestor path (one indexed query).
- `propose_term` — the one write path: a term the ontology doesn't confidently cover is enqueued onto the existing undecided path, where the normal REM re-debate machinery picks it up.

The same renderer backs retrieval text and the chat context block, so every consumer sees one idiom: MPL label primary, frame-grouped, provenance attached.

### Chat

`/api/chat` (and the `/chat` page) answers natural-language questions grounded in the live ontology — context selection by the shared scorer, frame-grouped prompts, MPL citations parsed back out for deep-linking, and tool-call action proposals (e.g. "X should be a child of Y") routed through the operator queue.

### Intent annotation

Beyond *what a term means*: *why the corpus deploys it* (speech-act illocution — teach, persuade, reassure, warn, …). Governed by hard guardrails (ADR-024/025/026): intent annotates definitions as source-deployment metadata; it never creates entries, never partitions an entry, never enters a label; intentionality is ordinal (low/medium/high), never a pseudo-precise float. All model-sourced tags pass an **N-pass unanimity gate** — a tag is stored only if every independent attribution pass proposes it, the ordinal only if all passes agree, and below-threshold attributions are withheld for operator review. The gate was validated empirically before rollout (15/15 unanimous attributions on real corpora, with minority tags and disagreed ordinals visibly dropped). New entries are attributed automatically at the pipeline tail; `backfill-intents` sweeps legacy definitions; retrieval filters by intent (`--intent teach`) without letting intent alter ranking.

## Quick start

```bash
# Install
git clone https://github.com/gellsmore-svg/mahalath
cd mahalath
python -m venv .venv && .venv/bin/pip install -e ".[dev,web]"

# Make sure MongoDB is running locally and Ollama has gemma4:e2b pulled
.venv/bin/mahalath db-ping
.venv/bin/mahalath show-config

# Process a document
cp my-source.md input/
.venv/bin/mahalath process-input --max-terms 10

# Browse the result
.venv/bin/mahalath list-ontology
.venv/bin/mahalath export-glossary --format md --out ontology/glossary.md

# Query it like an LLM would
.venv/bin/mahalath retrieve "substrate" --format text --budget 800
.venv/bin/mahalath subtree MPL-001 --depth 2
.venv/bin/mahalath propose-term "morphogenesis" --context "…source snippet…" --near MPL-004
```

For continuous operation, run the scheduler:

```bash
.venv/bin/mahalath run             # blocks; polls input/ every 60s, REM nightly at 02:00
.venv/bin/mahalath run --once      # cron-friendly: fire both jobs once and exit
```

For browser-based review + the JSON API:

```bash
.venv/bin/mahalath serve           # http://127.0.0.1:8000
# POST /api/chat          {question, focus_label?}
# POST /api/retrieve      {terms|labels, filters?, token_budget?, format?}
# POST /api/propose_term  {term, context?, near?, dry_run?}
```

For frontier-LLM review of the pending_review queue (operator-style adjudication by Claude):

```bash
export ANTHROPIC_API_KEY=sk-ant-…
.venv/bin/mahalath frontier-review --max-items 25
```

## Style overlay

A per-corpus voice-notes file makes definitions track the source's framing instead of generic dictionary fare:

```yaml
# config.yaml
runtime:
  style_overlay_path: docs/style-overlay.example.md
```

Per-document overrides:

```bash
.venv/bin/mahalath ingest-one input/book.md --style-overlay docs/voice-for-book.md
```

The overlay is injected into every agent prompt (extraction, debate, hierarchy review, redefine). In A/B testing this was the single biggest definition-quality lever.

## CLI overview

| Area | Commands |
|---|---|
| Pipeline | `ingest-one`, `process-document`, `process-input`, `db-ping`, `show-config` |
| Browse / export | `list-ontology`, `export-glossary`, `subtree` |
| Retrieval | `retrieve` (incl. `--intent`), `propose-term` |
| Hierarchy / proposals | `list-proposals`, `show-proposal`, `accept-proposal`, `reject-proposal`, `rollback-proposal` |
| Frames + intents | `list-contexts`, `add-context`, `show-context`, `seed-intents`, `backfill-contexts`, `backfill-intents` |
| Self-healing | `list-stale`, `audit-stale`, `redefine-stale`, `backfill-references`, `backfill-paths` |
| Review + serving | `frontier-review`, `serve`, `run` |

## Architecture

```
src/mahalath/
├── config.py            pydantic config tree, YAML loader
├── labels.py            MPL-NNN[.NNN][a-z]? label parse / format / successor helpers
├── ingestion.py         read + SHA-256 + archive + write document record
├── chunking.py          heading-aware Markdown chunker + per-chunk extraction
├── extraction.py        LLM-driven candidate term extraction
├── debate.py            multi-iteration debate loop, two agent roles, intent/valence guidance
├── ontology.py          persistence layer (entry + tree edge + decision log + queue)
├── actions.py           agent-callable structural actions (propose_parent / alias / merge / split)
├── hierarchy.py         post-accept hierarchy-review pass with N-of-N consensus
├── proposals.py         operator accept / reject / rollback workflow
├── rem.py               REM re-review of pending undecided items
├── frontier.py          frontier-LLM adjudicator over pending_review
├── staleness.py         reference tracking + staleness cascade + audit/redefine self-healing
├── paths.py             materialised ancestor paths (insert / re-parent / rollback maintenance)
├── retrieval.py         typed read view: search, codified refs, budgeted bundles, propose_term
├── intents.py           governed intent taxonomy (illocution tags) + idempotent seeder
├── chat.py              grounded NL Q&A over the ontology + tool-call action proposals
├── glossary.py          export to Markdown + JSON (frame-grouped), auto-refresh on change
├── scheduler.py         APScheduler harness (poll input/ + nightly REM)
├── style.py             per-corpus voice overlay loader
├── cli.py               argparse entry point
├── db/                  MongoDB layer (client, indexes, models, repositories)
├── adapters/            Adapter protocol + MockAdapter + OllamaCliAdapter + ClaudeApiAdapter
└── web/                 FastAPI dashboard + JSON API (chat / retrieve / propose_term)
```

Nine MongoDB collections: `documents`, `ontology_entries` (`_id` = MPL label), `ontology_tree`, `decision_log`, `agent_exchanges`, `undecided_queue`, `action_proposals`, `ontology_reviews`, `definition_contexts` (frames + intent tags).

## Design commitments

- **Labels are immutable and opaque** — re-parenting moves tree edges, never the label; no semantics in the key (ADR-018/021).
- **Human labels are approximate interfaces, not the ontology** (ADR-019).
- **Retrieval surfaces all frames; the caller disambiguates** (ADR-022). **Returned meanings are reference-closed** (ADR-023).
- **Intent annotates definitions; it never creates entries or enters labels** (ADR-024).
- **Everything is auditable** — every accepted definition links to its debate transcript; every structural change is a proposal with a rollback path.

The full decision record (26 ADRs + open questions) lives in `docs/architecture-decisions.md`; the retrieval design in `docs/retrieval-spec.md`; the intent extension in `docs/intent-extension-{discussion,evaluation}.md`.

## Status

Stage 2, deep. The full self-sustaining loop works end to end: ingest → debate → persist → hierarchy review → operator/frontier queues → REM re-review → staleness self-healing → glossary export — plus the complete retrieval layer (search / codified refs / budgeted bundles / subtree / propose-term, CLI + HTTP) and the complete intent extension (taxonomy, unanimity-gated attribution, intent-aware retrieval), validated against the live corpus and applied across all databases. **371 tests, all green**, including live MongoDB round-trips. Development history is chronicled slice-by-slice in `.session-log.md`.

## License

See LICENSE.
