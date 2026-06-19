---
type: Concept
title: Lexicon of meanings
description: Mahalath codifies meanings, not words. Each meaning gets an opaque, immutable label (e.g. MPL-004), one or more debated definitions, a place in a hierarchy, and a full audit trail; human terms are approximate interfaces onto these meanings.
resource: https://github.com/gellsmore-svg/mahalath/blob/main/docs/terminology.md
tags: [mahalath, lexicon, meanings, labels, mpl]
timestamp: 2026-06-19T00:00:00Z
---

# Lexicon of meanings

The premise: natural language is ambiguous, which makes AI-to-AI reasoning suffer.
Mahalath builds a precise **lexicon of meanings** rather than a dictionary of
words. Every meaning carries:

- an **opaque, immutable label** (e.g. `MPL-004`) drawn from one global sequence;
- one or more **debated, frame-tagged definitions** with a per-definition
  consensus score (see [debate](debate-and-consensus.md) and
  [frames](frames-and-polysemy.md));
- a place in the **[hierarchy](hierarchy.md)** (parent/child);
- a full **audit trail** ([provenance](provenance-and-staleness.md)).

**Human words are approximate interfaces** onto the lexicon's meanings: a single
term can hold several co-equal meanings. A consuming LLM retrieves by human term,
receives every codified meaning with provenance, and cites the `(MPL label, frame)`
pair it kept — see [retrieval](../modules/retrieval.md). A label addresses a
meaning *within its [language's lexicon](languages-and-mappings.md)*; there is no
language-independent "concept" node above the labels.
