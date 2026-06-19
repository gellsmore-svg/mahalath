---
type: Module
title: Ontology model
description: The lexicon's data and structure — the ontology store of meanings, opaque label allocation (MPL-NNN), the parent/child hierarchy, cross-language mapping assertions, and the auto-exported human-readable glossary.
resource: https://github.com/gellsmore-svg/mahalath/blob/main/src/mahalath/ontology.py
tags: [mahalath, ontology, glossary, labels, hierarchy]
timestamp: 2026-06-19T00:00:00Z
---

# Ontology model

The data and structure of the [lexicon](../concepts/lexicon-of-meanings.md):

- **`ontology.py`** — the store of meanings: frame-tagged definitions,
  per-definition consensus scores, status, and provenance.
- **`labels.py`** — allocation of the opaque, immutable `MPL-NNN`
  [labels](../concepts/lexicon-of-meanings.md) from the one global sequence.
- **`hierarchy.py`** — the parent/child [hierarchy](../concepts/hierarchy.md) and
  its multi-pass placement review.
- **`mappings.py`** — the cross-language
  [mapping assertions](../concepts/languages-and-mappings.md) (ADR-029).
- **`glossary.py`** — auto-export to `ontology/glossary.{md,json}`, a
  human-readable view of the lexicon.

This is what the [build pipeline](pipeline.md) writes, [retrieval](retrieval.md)
reads, and [maintenance](maintenance.md) keeps consistent.
