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

- [x] All tests green; live MongoDB round-trips pass. (437 passing.)
- [x] `mahalath init` verified on a clean database (collections, indexes,
      taxonomies) — the documented fresh-install path.
- [x] **Live embedding verification**: `bge-m3` pulled; `backfill-embeddings
      --apply` ran end-to-end against real Ollama (117/119 embedded); the
      embedding shortlist recovers the planted de↔en pairs the prompt
      stage missed — `coupling` ranks #1 for Kopplung, `phase` #1 for
      Phasenverschiebung (2026-06-13).
- [x] WSL reboot-robustness: `ollama_base_url` `wsl-gateway` sentinel
      auto-resolves the Windows gateway IP at startup.
- [x] `README.md` fresh-install quickstart updated (install → `init` →
      process → mappings → serve), incl. the WSL embedding note.
- [x] `config.example.yaml` carries the embedding + candidate-source knobs.
- [x] Version bumped to `1.0.0` in `pyproject.toml`.
- [ ] Git tag `v1.0.0` (operator-gated — left for the operator to cut).
- [x] `.restart.md` updated to mark v1 closed and v2 candidates carried.

## Known limitations (accepted for v1)

- **Two RS entries (MPL-055 mode exchange, MPL-059 electricity) are
  un-embeddable**: bge-m3 emits a non-finite (NaN) value for their text on
  every input variant tried. They are cleanly skipped (recorded as
  `skipped_nan`) and simply don't appear in embedding shortlists; in
  `auto` candidate mode the prompt stage still covers them. Re-wording
  their definitions, or a different embedding model, would recover them.

## Open items (post-v1, non-blocking)

1. **Re-audit of the stale de↔en mappings** — 24 mappings were flagged
   stale after the S2.50 German re-debate. `generate-mappings` skips pairs
   that already hold a mapping, so re-auditing needs the staleness/audit
   path, not regeneration — a small dedicated pass (v1.x).
2. **`top_k` default** — 5 misses known-good pairs sitting just outside it
   (Rückkopplung→Feedback at rank 8); raising it trades gate cost for
   recall. Left at 5; tune per run via `--top-k`.
