# Mahalath

Self-sustaining multi-agent ontology builder. Drop Markdown documents into `input/`, walk away, and come back to a definitionally-sharp glossary with parent/child relationships, full provenance, and operator-reviewable proposal queues.

The system is **local-first** (MongoDB + Ollama by default) with optional **frontier-LLM review** (Anthropic Claude API) for queue items the local model isn't confident about.

## What it does

```
input/file.md  →  ingest + SHA-256 dedupe + archive
              →  heading-aware chunk (any document size)
              →  LLM-driven candidate term extraction
              →  multi-iteration debate (PrecisionCritic + SynthesisExplorer)
              →  if accepted: ontology entry + hierarchy review pass (3-pass consensus)
              →  if undecided: queue for nightly REM re-debate
              →  glossary auto-export to ontology/glossary.{md,json}
```

Every model interaction is recorded with a `decision_log_id` and queryable forever. Every operator action (accept, reject, rollback) writes back to the same audit chain.

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
```

For continuous operation, run the scheduler:

```bash
.venv/bin/mahalath run             # blocks; polls input/ every 60s, REM nightly at 02:00
.venv/bin/mahalath run --once      # cron-friendly: fire both jobs once and exit
```

For browser-based review:

```bash
.venv/bin/mahalath serve           # http://127.0.0.1:8000
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

The overlay is injected into every agent prompt (extraction, debate, hierarchy review).

## Architecture

```
src/mahalath/
├── config.py            pydantic config tree, YAML loader
├── labels.py            MPL-NNN[.NNN][a-z]? label parse / format / successor helpers
├── ingestion.py         read + SHA-256 + archive + write document record
├── chunking.py          heading-aware Markdown chunker + per-chunk extraction
├── extraction.py        LLM-driven candidate term extraction
├── debate.py            multi-iteration debate loop, two agent roles
├── ontology.py          persistence layer (entry + tree edge + decision log + queue)
├── actions.py           agent-callable structural actions (propose_parent / alias / merge / split)
├── hierarchy.py         post-accept hierarchy-review pass with N-of-N consensus
├── proposals.py         operator accept / reject / rollback workflow
├── rem.py               REM re-review of pending undecided items
├── frontier.py          frontier-LLM adjudicator over pending_review
├── staleness.py         reference tracking + dependent invalidation on upstream change
├── glossary.py          export to Markdown + JSON, auto-refresh on ontology change
├── scheduler.py         APScheduler harness (poll input/ + nightly REM)
├── style.py             per-corpus voice overlay loader
├── cli.py               argparse entry point
├── db/                  MongoDB layer (client, indexes, models, repositories)
├── adapters/            Adapter protocol + MockAdapter + OllamaCliAdapter + ClaudeApiAdapter
└── web/                 optional FastAPI dashboard
```

## Status

Stage 2. The full self-sustaining loop works: ingest → debate → persist → hierarchy review → operator/frontier queue → REM re-review → glossary export. 219 tests, all green. See `docs/architecture-decisions.md` for ADRs.

## License

See LICENSE.
