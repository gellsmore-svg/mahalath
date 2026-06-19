---
type: Project
title: Mahalath
description: A self-sustaining multi-agent ontology builder — drop Markdown into input/, and it builds a definitionally-sharp lexicon of meanings (opaque labels, debated definitions, hierarchy, full provenance, operator-reviewable queues) plus a retrieval API that lets another LLM reason in precise internal terms.
resource: https://github.com/gellsmore-svg/mahalath
tags: [mahalath, ontology, lexicon, multi-agent, local-first]
timestamp: 2026-06-19T00:00:00Z
---

# Mahalath

Mahalath is a **self-sustaining multi-agent ontology builder**. Drop Markdown
documents into `input/`, walk away, and come back to a definitionally-sharp
**lexicon of meanings** with parent/child relationships, polysemy-aware
definitions, full provenance, and operator-reviewable proposal queues — plus a
retrieval API that lets another LLM reason in the ontology's precise internal terms
instead of ambiguous natural language.

It is **local-first** (MongoDB + Ollama) with optional **frontier-LLM review**
(Anthropic Claude) for items the local model isn't confident about.

This bundle is an [Open Knowledge Format](https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing)
description of Mahalath's concepts and modules.

## Map

- **[Concepts](concepts/index.md)** — the lexicon of meanings, frames & polysemy,
  agent debate, hierarchy, provenance & staleness, operator review, languages &
  mappings, and the local-first/frontier split.
- **[Modules](modules/index.md)** — the code: the build pipeline, the ontology
  model, retrieval, maintenance, and frontier/config.

## At a glance

- Every meaning gets an **opaque, immutable label** (`MPL-004`), one or more
  **debated, frame-tagged definitions**, a place in a hierarchy, and a full audit
  trail — see [lexicon of meanings](concepts/lexicon-of-meanings.md).
- A consuming LLM **retrieves by human term**, receives every codified meaning with
  provenance, and cites the `(MPL label, frame)` pair it kept.
- Autonomous: ingest → chunk → extract → [debate](concepts/debate-and-consensus.md)
  → accept/queue → nightly REM re-debate → reference/[staleness](concepts/provenance-and-staleness.md)
  self-healing → glossary export.
- License: Apache-2.0.
