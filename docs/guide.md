# Mahalath guide

Everything beyond the front door: the capabilities in detail, operator workflows, the style overlay, the full CLI, the code layout and Hoglah routing. Moved from the root README (2026-10). Run `mahalath <command> --help` for any command's options.

## Capabilities in detail

### Two prose layers per definition

Each definition carries two layers of prose for the **same sense**: a short, multi-agent-debated `text` (the precise sense used for identity, consensus, and reference extraction) and an optional longer `detailed_text` exposition generated after accept for glossary readers and richer retrieval. Detailed text is not a second meaning or frame — it elaborates the accepted short definition. Generate on write with `runtime.generate_detailed_definitions` (default on), or fill a live corpus with `mahalath backfill-detailed --apply`.

### Polysemy as a first-class citizen

Definitions are tagged with a **context frame** (a governed taxonomy of `DefinitionContext` rows, authored per corpus). One entry can legitimately carry, say, a legal definition *and* an engineering one — they are co-equal; nothing supersedes anything. The web UI, chat, glossary export, and retrieval layer all group and label definitions by frame.

**`consensus_score` is pathway-specific.** The multi-agent debate path records a per-definition consensus score (min confidence across PrecisionCritic / SynthesisExplorer). The REM redefine path leaves it `null` while the stored wording is that single-model verdict. When overnight grounding is on and the explorer's revision receives exact-wording critic ratification, the stored score is the minimum across the grounding round. Operator-authored definitions have no consensus score. Older definitions may lack the field entirely (schema evolution); treat absence/`null` as "not a debate consensus," not as zero.

### External semantic grounding

The source document stays the primary evidence for what a term means in that document. `semantic_grounding.mode` chooses whether anything outside the corpus may be consulted after the ordinary debate:

- `corpus_only` (default) supplies no retrieved evidence. Use it for a corpus that should be read as self-defining.
- `hybrid` runs the corpus debate first and looks outward only when that debate's minimum confidence is strictly below `external_lookup_threshold` (default 8.0).
- `external` always looks up after the corpus debate.

Passages can clarify, discriminate, corroborate, challenge, or enrich. A revised definition is stored only when SynthesisExplorer proposes it and PrecisionCritic ratifies that exact wording as faithful to the source, neither dismisses or challenges the passages, the wording is not copied from an external passage, and every grounding score is at least as high as the corpus debate's. A challenge keeps the corpus wording and cannot raise confidence. A failed lookup or model call leaves the corpus decision, with failed model responses retained in the audit trail. The passages are an audit of that decision (`semantic_grounding`, plus a snapshot on the definition), not a web glossary. Retrieval, the glossary, and the entry page show the relation and how many sources were considered. Configure sources under `semantic_grounding.providers`: a local JSON file (`static`), MediaWiki search snippets (`wikipedia`), or an operator endpoint (`http`).

Stale definitions rewritten by the overnight REM job, and by `redefine-stale`, stay inside the corpus unless `semantic_grounding.overnight` is turned on. It defaults to off. With it on, that rewrite is still taken from the ontology first, and external lookup then follows the same mode and threshold. A result that does not clear the redefine confidence floor is not stored. REM re-debate of items still below the debate threshold follows `mode` on its own; the overnight flag does not gate it.

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

The system periodically audits its own decision-making (`analysis.py`, read-only over the audit trails). The headline is **calibration**: every operator accept/reject/rollback on an agent proposal is a labelled data point for the confidence the agent stated when proposing — if operator acceptance doesn't rise with agent confidence, the threshold knobs are tuning noise, and the report says so in plain language. Also covered: debate outcome/iteration stats, REM re-debate resolution arcs (undecided → later accepted), undecided-queue health (items stuck at max escalation), hierarchy-review yield, and frame/intent coverage. `iterations_used` is the count of numbered transcript rounds, so a grounding round adds one to the persisted value and effectiveness averages; the in-memory corpus count remains separate for retry-cap decisions. Surfaced as `mahalath effectiveness` (text or JSON), the `/effectiveness` web page, `GET /api/effectiveness`, and a nightly JSON-line snapshot appended to `logs/effectiveness.jsonl` by the REM job.

## Operator workflows

### Understanding how a term was produced

Every model call that contributes prose to a term — the debate behind the short
definition, and the expansion behind `detailed_text` — records the full
conversation (`decision_log` + `agent_exchanges`), linked from the definition
(ADR-034). Read it with `mahalath show-decision <MPL-label|decision-log-id>`, or
from the entry page in the web UI, which lists every conversation and links to a
view of each.

### Review only what is actually stuck

The operator is asked about a term only once the system has finished trying
(ADR-037): below `runtime.confidence_threshold` it re-debates overnight, and it
surfaces after two retries if it is still short. `conflict` and
`moderator_block` — where the agents disagree on whether a term holds one
meaning or two — surface immediately, because more recursion does not settle
them. `mahalath needs-review` and the `/undecided` page show that set, with
accept/reject writing to the audit chain.

### Comparing runs over related material

`mahalath link-documents <document-id>` asks the model whether an incoming
document is related to one already processed — a revision, translation, excerpt
or shared material — and records the link. This is **not** deduplication: the
document is processed in full and the original's terms are untouched.
`--correspond` then matches terms across the pair, and
`mahalath compare-documents <link-id>` reports what differs — shared terms,
terms unique to each side, and definitions that changed. That is how you answer
whether a different model or a changed process improved the output (ADR-036).
Opt in at ingest with `runtime.link_related_documents`.

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
| Setup + maintenance | `init`, `migrate`, `backfill-language`, `dedupe-definitions` |
| Operator review | `needs-review`, `accept-undecided`, `reject-undecided`, `show-decision`, `redebate-entry`, `debate-one` |
| Related documents | `link-documents`, `compare-documents` |
| Detailed definitions | `backfill-detailed` |
| Cross-language mappings | `seed-mapping-relations`, `backfill-embeddings`, `generate-mappings`, `list-mappings`, `resolve-mapping` |

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
constrained GPU). Hoglah's own [known limits](https://github.com/gellsmore-svg/hoglah#know-before-you-rely-on-it) apply.

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

## Design notes and reviews

**Scholarly layer + same-document reasoning memory (design, ADR-033):**
[`docs/scholarly-layer.md`](scholarly-layer.md) — three prose layers per
sense (`text` / `detailed_text` / scholarly), full debate transcripts as ground
truth, distilled lesson cards injected into later debates. **Past thinking is
scoped to the same source document only** (not other corpora). Implementation
pending.

A point-in-time functional + code review of 1.2.0 is in
[`docs/review-2026-08-08.md`](review-2026-08-08.md). Findings H1–H3 / F1–F6 /
M1 / M4 are actioned in **1.3.0**; detailed expositions shipped in **1.4.0**.

A follow-up review of 1.4.0 `detailed_text` and the ADR-033 scholarly design is in
[`docs/review-2026-08-10-detailed-and-scholarly.md`](review-2026-08-10-detailed-and-scholarly.md).
It also records four operator requirements from 2026-08-10: viewable conversation
history per prose layer, no backfill for new definitions, related-document term
traceability, and operator review gated on confidence after recursion.
