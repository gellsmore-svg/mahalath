# Mahalath

Self-sustaining multi-agent ontology builder. Drop Markdown documents into `input/`, walk away, and come back to a definitionally-sharp glossary with parent/child relationships, polysemy-aware definitions, full provenance, and operator-reviewable proposal queues — plus a retrieval API that lets another LLM reason in the ontology's precise internal terms instead of ambiguous natural language.

The system is **local-first** (MongoDB + Ollama by default) with optional **frontier-LLM review** (Anthropic Claude API) for queue items the local model isn't confident about.

## The idea

Natural language is ambiguous; AI-to-AI reasoning suffers for it. Mahalath ingests a corpus — any corpus: a legal code, an engineering handbook, a research field's literature, a novel — and continuously refines a precise **lexicon of meanings**: every meaning gets an opaque, immutable label (`MPL-004`), one or more debated definitions, a place in a hierarchy, and a full audit trail. Human words are approximate interfaces onto the lexicon's meanings — a single term can hold several co-equal meanings, each keyed by the *frame* it speaks within (a `field` is one thing to a physicist, another to a farmer, another to a database designer). A consuming LLM retrieves by human term, receives every codified meaning with provenance, and cites the `(MPL label, frame)` pair it kept.

Each definition carries two layers of prose for the **same sense**: a short, multi-agent-debated `text` (the precise sense used for identity, consensus, and reference extraction) and an optional longer `detailed_text` exposition generated after accept for glossary readers and richer retrieval. Detailed text is not a second meaning or frame — it elaborates the accepted short definition. Generate on write with `runtime.generate_detailed_definitions` (default on), or fill a live corpus with `mahalath backfill-detailed --apply`.

A lexicon belongs to **one language** (the live one is English). Languages are discrete peers, never derived from each other: a German lexicon would be built from German evidence with its own tree, because terms across languages are rarely, if ever, like-for-like — which is the reason the system exists. Labels are opaque and drawn from one global sequence, but each addresses a meaning *within its language's lexicon*; there is no language-independent "concept" node above them. Cross-language relationships, when built (ADR-028–030, phased on the backlog), are explicit, weighted, debated **mapping assertions** — supporting translation drafting/review and cross-language comparison of *illocution* (how each language deploys the term, which is part of its meaning) — never translation at ingestion.

## What it does

```
input/file.md  →  ingest + SHA-256 dedupe + archive
              →  heading-aware chunk (any document size)
              →  LLM-driven candidate term extraction
              →  multi-iteration debate (PrecisionCritic + SynthesisExplorer)
              →  if accepted: ontology entry (frame-tagged definition; debate path
                 records a per-definition consensus score) + hierarchy review
                 pass (3-pass consensus)
              →  if undecided: queue for nightly REM re-debate
              →  reference extraction → reverse index → staleness cascade when
                 upstream entries change → audit/redefine self-healing
              →  glossary auto-export to ontology/glossary.{md,json}
```

Every model interaction is recorded with a `decision_log_id` and queryable forever. Every operator action (accept, reject, rollback) writes back to the same audit chain.

### Polysemy as a first-class citizen

Definitions are tagged with a **context frame** (a governed taxonomy of `DefinitionContext` rows, authored per corpus). One entry can legitimately carry, say, a legal definition *and* an engineering one — they are co-equal; nothing supersedes anything. The web UI, chat, glossary export, and retrieval layer all group and label definitions by frame.

**`consensus_score` is pathway-specific.** The multi-agent debate path records a per-definition consensus score (min confidence across PrecisionCritic / SynthesisExplorer). The REM redefine path deliberately leaves it `null` — it is a single-model verdict, not multi-agent agreement — and operator-authored definitions likewise have no consensus score. Older definitions may lack the field entirely (schema evolution); treat absence/`null` as "not a debate consensus," not as zero.

### Self-healing

Each entry records which other MPL labels its definitions mention (explicit + semantic matching, maintained incrementally on insert). When an upstream entry changes — redefined, re-parented, rolled back — every dependent is flagged stale, cascading with a cycle guard. Nightly REM jobs re-audit stale entries against current upstream state and either clear the flag or re-debate the definition.

### Retrieval layer (for an orchestrating LLM)

A typed read view over the ontology (`retrieval.py`), available as a library, CLI, and HTTP API:

- `search_terms` — resolve human terms to ranked matches (shared scorer + `$text` fuzzy index, branch/frame/status/confidence filters).
- `get_codified` — expand `MPL-004` (or the frame-scoped handle `MPL-004#physics`) into all meanings, tree path, references both directions, provenance, stale state.
- `build_bundle` — a **token-budgeted, prompt-ready bundle**: primary entries with *all* their frames (retrieval never collapses polysemy — the caller disambiguates), a mandatory **reference closure** (every codified term cited inside a returned description is included transitively, cycle-safe), ranked alternatives, and a compact NL rendering. Budget pressure trims breadth and verbosity in recorded steps; it never drops a frame or a closure node.
- `subtree` — limited-depth descendant summaries via the materialised ancestor path (one indexed query).
- `propose_term` — the one write path: a term the ontology doesn't confidently cover is enqueued onto the existing undecided path, where the normal REM re-debate machinery picks it up.

The same renderer backs retrieval text and the chat context block, so every consumer sees one idiom: MPL label primary, frame-grouped, provenance attached.

### Chat

`/api/chat` (and the `/chat` page) answers natural-language questions grounded in the live ontology — context selection by the shared scorer, frame-grouped prompts, MPL citations parsed back out for deep-linking, and tool-call action proposals (e.g. "X should be a child of Y") routed through the operator queue.

### Intent annotation

Beyond *what a term means*: *why the corpus deploys it* (speech-act illocution — teach, persuade, reassure, warn, …). Governed by hard guardrails (ADR-024/025/026): intent annotates definitions as source-deployment metadata; it never creates entries, never partitions an entry, never enters a label; intentionality is ordinal (low/medium/high), never a pseudo-precise float. All model-sourced tags pass an **N-pass unanimity gate** — a tag is stored only if every independent attribution pass proposes it, the ordinal only if all passes agree, and below-threshold attributions are withheld for operator review. The gate was validated empirically before rollout (15/15 unanimous attributions on real corpora, with minority tags and disagreed ordinals visibly dropped). New entries are attributed automatically at the pipeline tail; `backfill-intents` sweeps legacy definitions; retrieval filters by intent (`--intent teach`) without letting intent alter ranking.

### Decision-effectiveness self-analysis

The system periodically audits its own decision-making (`analysis.py`, read-only over the audit trails). The headline is **calibration**: every operator accept/reject/rollback on an agent proposal is a labelled data point for the confidence the agent stated when proposing — if operator acceptance doesn't rise with agent confidence, the threshold knobs are tuning noise, and the report says so in plain language. Also covered: debate outcome/iteration stats, REM re-debate resolution arcs (undecided → later accepted), undecided-queue health (items stuck at max escalation), hierarchy-review yield, and frame/intent coverage. Surfaced as `mahalath effectiveness` (text or JSON), the `/effectiveness` web page, `GET /api/effectiveness`, and a nightly JSON-line snapshot appended to `logs/effectiveness.jsonl` by the REM job.

## Quick start

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
```

### Cross-language mappings (multilingual lexicons)

Mappings relate a term in one language to a term in another by *meaning*.
Candidates are found with meaning-fingerprints (embeddings), so the
embedding model must be pulled (`bge-m3`) and reachable.

```bash
# Compute a fingerprint for every entry (dry-run first; --apply writes).
.venv/bin/mahalath backfill-embeddings --apply

# Generate mappings between two lexicons (dry-run by default).
.venv/bin/mahalath generate-mappings --source-language de --target-language en
.venv/bin/mahalath generate-mappings --source-language de --target-language en --apply
.venv/bin/mahalath list-mappings --status accepted
```

> **Running under WSL2?** Generation reaches Ollama via the CLI, but
> embeddings use Ollama's HTTP API, which from WSL means the Windows host,
> not `localhost`. Set `OLLAMA_HOST=0.0.0.0` on the Windows side, restart
> Ollama, and set `ollama_base_url: http://wsl-gateway:11434` in
> `config.yaml` — the `wsl-gateway` host auto-resolves to the Windows
> gateway and survives WSL restarts.

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
| Self-analysis | `effectiveness` (incl. `--format json`, `--snapshot`) |
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
├── analysis.py          decision-effectiveness self-analysis (§3.4): calibration, queue health, findings
├── intents.py           governed intent taxonomy (illocution tags) + idempotent seeder
├── chat.py              grounded NL Q&A over the ontology + tool-call action proposals
├── glossary.py          export to Markdown + JSON (frame-grouped), auto-refresh on change
├── scheduler.py         APScheduler harness (poll input/ + nightly REM)
├── style.py             per-corpus voice overlay loader
├── cli.py               argparse entry point
├── db/                  MongoDB layer (client, indexes, models, repositories)
├── adapters/            Adapter protocol + MockAdapter + OllamaCliAdapter + ClaudeApiAdapter + HoglahAdapter
└── web/                 FastAPI dashboard + JSON API (chat / retrieve / propose_term)
```

Nine MongoDB collections: `documents`, `ontology_entries` (`_id` = MPL label), `ontology_tree`, `decision_log`, `agent_exchanges`, `undecided_queue`, `action_proposals`, `ontology_reviews`, `definition_contexts` (frames + intent tags).

## Routing via Hoglah (queue daemon)

By default Mahalath calls Ollama over HTTP (`model_adapter: ollama_http`; no
`ollama` binary required on PATH). For a
walk-away run you can instead route **both generation and embeddings** through
[Hoglah](https://github.com/gellsmore-svg/hoglah), a local-first job queue, so
every model call is serialized through one durable queue (handy on a single
constrained GPU) and survives restarts.

```bash
pip install 'mahalath[hoglah]'
```

Set the adapter(s) to `hoglah` in `config.yaml` and configure the
`runtime.hoglah` block (see `config.example.yaml`). Then run a **separate**
Hoglah worker daemon pointed at the same queue + output folder:

```bash
HOGLAH_OUTPUT_DIR=~/.hoglah/outbox hoglah run --real   # executes jobs vs Ollama
```

Mahalath becomes a pure submitter: it enqueues each call and gets the result
back either by polling the output folder (`delivery: poll`) or via an HTTP
callback to a tiny receiver it runs (`delivery: callback`, with poll as
fallback). Mahalath owns the callback URL and sends it to Hoglah per job —
nothing about Mahalath is baked into Hoglah, so the same mechanism works for
any caller. Embedding/fingerprinting routes through Hoglah's embedding jobs
(`bge-m3`).

**Messaging transports.** Instead of the shared SQLite store, Mahalath can submit
over a broker — set `runtime.hoglah.transport` to `kafka`, `rabbitmq`, or `redis`
(default `store`). It then publishes a job-request message and awaits the result
over the same broker via Hoglah's `MessagingSubmitter`; a matching
`hoglah {kafka,rabbitmq,redis}-bridge` worker must run on the configured
topics/queues/streams. Install the broker client with the matching extra:
`pip install 'mahalath[hoglah-kafka]'` (or `hoglah-rabbitmq` / `hoglah-redis`).

## Design commitments

- **Labels are immutable and opaque** — re-parenting moves tree edges, never the label; no semantics in the key (ADR-018/021).
- **Human labels are approximate interfaces, not the ontology** (ADR-019).
- **Languages are discrete peer lexicons** — a label addresses a meaning within one language's lexicon; cross-language equivalence is only ever an explicit, debated mapping assertion, and locale is metadata, never structure (ADR-028/029/030).
- **Retrieval surfaces all frames; the caller disambiguates** (ADR-022). **Returned meanings are reference-closed** (ADR-023).
- **Intent annotates definitions; it never creates entries or enters labels** (ADR-024).
- **Everything is auditable** — every accepted definition links to its debate transcript; every structural change is a proposal with a rollback path.

- **Self-analysis is read-only and file-snapshotted** — the effectiveness layer aggregates the audit trails it reports on but can never write to them (ADR-027).

The full decision record (ADRs + open questions) lives in
[`docs/architecture-decisions.md`](docs/architecture-decisions.md); the retrieval
design in [`docs/retrieval-spec.md`](docs/retrieval-spec.md); the intent
extension in `docs/intent-extension-{discussion,evaluation}.md`.

**Scholarly layer + same-document reasoning memory (design, ADR-033):**
[`docs/scholarly-layer.md`](docs/scholarly-layer.md) — three prose layers per
sense (`text` / `detailed_text` / scholarly), full debate transcripts as ground
truth, distilled lesson cards injected into later debates. **Past thinking is
scoped to the same source document only** (not other corpora). Implementation
pending.

A point-in-time functional + code review of 1.2.0 is in
[`docs/review-2026-08-08.md`](docs/review-2026-08-08.md). Findings H1–H3 / F1–F6 /
M1 / M4 are actioned in **1.3.0**; detailed expositions shipped in **1.4.0**.

## Status

Stage 2, deep. The full self-sustaining loop works end to end: ingest → debate → persist → hierarchy review → operator/frontier queues → REM re-review → staleness self-healing → glossary export — plus the complete retrieval layer (search / codified refs / budgeted bundles / subtree / propose-term, CLI + HTTP), the complete intent extension (taxonomy, unanimity-gated attribution, intent-aware retrieval), and nightly decision-effectiveness self-analysis (§3.4), validated against the live corpus and applied against the live lexicon. The multilingual architecture is accepted and phased (ADR-028–030); the live lexicon is English. **398 tests, all green**, including live MongoDB round-trips. Development history is chronicled slice-by-slice in `.session-log.md`.

## Knowledge bundle

A machine- and human-readable knowledge map of Mahalath's concepts and modules is
published as an [Open Knowledge Format](https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing)
bundle under [`okf/`](okf/index.md) — markdown with YAML frontmatter, linked into a
concept graph.

## License

See LICENSE.
