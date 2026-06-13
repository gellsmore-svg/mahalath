# Mahalath v1 — Scope and Release Checklist

This file defines what a finished **v1** of Mahalath is: what's in, what's
explicitly held for later, and the checklist to cut the release. It is the
contract for "done" so v1 can be tagged and installed from a clean machine.

## What v1 is

A local-first system that ingests documents, debates precise definitions
across multiple local models, grows and self-heals a multilingual ontology,
answers grounded questions over it, serves a retrieval API, and asserts
cross-language mappings between lexicons — all against MongoDB + a local
Ollama install, with no required external API.

## In scope for v1 (built and proven)

- **Ingestion**: watched-folder + single-file, SHA-256 dedupe, source
  preserved verbatim to `processed/`.
- **Debate → ontology**: cross-family PrecisionCritic/SynthesisExplorer
  debate; MPL-labelled entries; hierarchy; decision-log + agent-exchange
  audit; undecided queue.
- **Polysemy & intent**: definition frames; consensus-gated intent
  attribution.
- **Self-healing**: staleness cascade, audit, redefine, and re-debate of
  accepted entries (quality refresh).
- **Multilingual (M-A/M-B)**: per-lexicon `language`; German pilot live;
  cross-language guard/search boundaries.
- **Cross-language mappings (M-C)**: governed relationship taxonomy,
  majority-verdict attribution gate with operator resolution, illocution
  comparison, staleness participation.
- **Candidate shortlisting via meaning-fingerprints**: multilingual
  embeddings + similarity shortlist replacing the prompt-based candidate
  stage (`backfill-embeddings`, `generate-mappings --candidate-source`).
- **Retrieval**: typed read view, token-budgeted bundles, `propose-term`.
- **Self-analysis**: decision-effectiveness report + nightly snapshots.
- **Serving**: FastAPI web UI + JSON API; REM nightly consolidation.
- **Fresh-install bootstrap**: `mahalath init` self-creates every
  collection + index and seeds the standard taxonomies (idempotent).
- **Multi-model**: per-role debate models + cross-family consensus roster.

## Explicitly NOT in v1 (held for v2+)

- **Tirzah context-collation integration** (ADR-031) — optional synchronous
  ingestion-time context enrichment. v2.
- **Debate self-check / verification turn** — a critique step to catch
  grammar/logic slips at creation (the quality lever for the residual
  fluency issues). Candidate for v1.x or v2.
- **Frontier-model selective healing** — route high-value/low-confidence
  entries to a frontier model. Needs an API key + cost decision.
- **Non-LLM grammar gate** for non-English lexicons.
- **Embeddings as a `search_terms` backend** (the original S-E retrieval
  idea) — the layer now exists for M-C and could extend here.
- **Dynamic per-problem model selection** (deprioritised).

## Release checklist (to execute when cutting v1.0.0)

- [ ] All tests green; live MongoDB round-trips pass.
- [ ] `mahalath init` verified on a clean database (collections, indexes,
      taxonomies) — the documented fresh-install path.
- [ ] **Live embedding verification**: `bge-m3` pulled; `backfill-embeddings
      --apply` runs end-to-end against real Ollama; `generate-mappings
      --candidate-source embedding` recovers the planted de↔en pairs
      (MPL-039 phase, MPL-038 coupling) the prompt stage missed. *(This is
      the one capability built but not yet proven on live hardware — it
      needs Ollama HTTP embed reachable from WSL + the model pulled.)*
- [ ] `README.md` fresh-install quickstart updated (install → `init` →
      ingest → process → serve).
- [ ] `config.example.yaml` carries the embedding + candidate-source knobs
      (commented) and a working default.
- [ ] Version bumped to `1.0.0` in `pyproject.toml`.
- [ ] `CHANGELOG.md` written (or `.session-log.md` referenced as history).
- [ ] Git tag `v1.0.0` (operator-gated).
- [ ] `.restart.md` updated to mark v1 closed and v2 candidates carried.

## Open items that gate "done"

1. **Live embedding path** — the only built-but-unproven capability;
   depends on `bge-m3` pulled and Ollama's HTTP embed endpoint reachable
   from WSL. If HTTP-from-WSL doesn't work, decide a fallback (port proxy,
   or a CLI embed route) before v1.
2. **Re-audit of the 24 stale de↔en mappings** — flagged after the S2.50
   German re-debate; should run once the live embedding shortlist lands so
   improved definitions and improved recall land together.
