---
type: Module
title: Build pipeline
description: The path from a dropped document to proposed meanings — ingest + SHA-256 dedupe + archive, heading-aware chunking at any size, LLM-driven candidate term extraction, multi-iteration agent debate, and operator-facing action proposals.
resource: https://github.com/gellsmore-svg/mahalath/blob/main/src/mahalath/extraction.py
tags: [mahalath, pipeline, ingestion, extraction, debate]
timestamp: 2026-06-19T00:00:00Z
---

# Build pipeline

The path from `input/file.md` to proposed [meanings](../concepts/lexicon-of-meanings.md):

- **`ingestion.py`** — ingest + SHA-256 dedupe + archive; evidence enters in its own
  [language](../concepts/languages-and-mappings.md).
- **`chunking.py`** — heading-aware chunking, robust to any document size.
- **`extraction.py`** — LLM-driven candidate **term extraction** from chunks.
- **`debate.py`** — the multi-iteration [debate](../concepts/debate-and-consensus.md)
  (PrecisionCritic + SynthesisExplorer) that turns a candidate into a frame-tagged,
  consensus-scored definition.
- **`proposals.py`** — the [operator-review](../concepts/operator-review.md)
  workflow over the resulting action proposals.

Accepted candidates become [ontology entries](ontology-model.md) and get a
[hierarchy](../concepts/hierarchy.md) pass; undecided ones go to
[REM](maintenance.md). The pipeline is orchestrated by the
[scheduler](maintenance.md).
