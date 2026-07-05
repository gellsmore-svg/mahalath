---
type: Module Index
title: Mahalath Modules
description: The code, grouped by role — the build pipeline (ingest → extract → debate → propose), the ontology model, retrieval for consuming LLMs, the autonomous maintenance loop, and frontier/config.
resource: https://github.com/gellsmore-svg/mahalath/tree/main/src/mahalath
tags: [mahalath, modules, code]
timestamp: 2026-06-19T00:00:00Z
---

# Modules

The ~24 modules under `src/mahalath/`, grouped by role:

- **[Build pipeline](pipeline.md)** — `ingestion`, `chunking`, `extraction`,
  `debate`, `proposals`: input → candidate meanings → consensus → proposals.
- **[Ontology model](ontology-model.md)** — `ontology`, `glossary`, `labels`,
  `hierarchy`, `mappings`: the lexicon's data and structure.
- **[Retrieval](retrieval.md)** — `retrieval`, `embeddings`, `intents`, `chat`: how
  a consuming LLM queries the ontology.
- **[Maintenance](maintenance.md)** — `scheduler`, `rem`, `staleness`, `actions`,
  `analysis`: the autonomous self-healing loop.
- **[Frontier & config](frontier-config.md)** — `frontier`, `config`, `style`,
  `paths`.

The CLI (`cli.py`, e.g. `db-ping`, `show-config`) is a thin operator front-end;
most work is driven autonomously by the [scheduler](maintenance.md).
- **[web](web.md)** — the local operator browser and its API seams.
- **[tracing](tracing.md)** — the Galeed spine witness for the pipeline.

