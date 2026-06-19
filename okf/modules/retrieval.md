---
type: Module
title: Retrieval
description: How a consuming LLM queries the ontology — retrieve by human term to receive every codified meaning with provenance, backed by embeddings, intent handling, and a chat surface, so another model can reason in the lexicon's precise internal terms.
resource: https://github.com/gellsmore-svg/mahalath/blob/main/src/mahalath/retrieval.py
tags: [mahalath, retrieval, embeddings, chat, api]
timestamp: 2026-06-19T00:00:00Z
---

# Retrieval

The consumer-facing side — the reason the [lexicon](../concepts/lexicon-of-meanings.md)
exists: let another LLM reason in **precise internal terms** instead of ambiguous
natural language (`docs/retrieval-spec.md`).

- **`retrieval.py`** — programmatic retrieval over the ontology: query by **human
  term**, receive **every codified [meaning](../concepts/frames-and-polysemy.md)**
  with provenance, so the consumer can cite the `(MPL label, frame)` pair it kept.
- **`embeddings.py`** — vector embeddings supporting similarity/lookup.
- **`intents.py`** — intent handling over queries.
- **`chat.py`** — a chat surface for interacting with the ontology.

This is the natural integration point with the family's memory layer
([Tirzah](https://github.com/gellsmore-svg/tirzah/blob/main/okf/index.md)): a
consuming reasoner retrieves disambiguated meanings here and grounds its answers in
them.
