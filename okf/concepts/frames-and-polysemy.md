---
type: Concept
title: Frames & polysemy
description: A single human term can hold several co-equal meanings, each keyed by the frame it speaks within — a "field" is one thing to a physicist, another to a farmer, another to a database designer. Definitions are frame-tagged so polysemy is preserved, not collapsed.
resource: https://github.com/gellsmore-svg/mahalath/blob/main/docs/terminology.md
tags: [mahalath, frames, polysemy, definitions]
timestamp: 2026-06-19T00:00:00Z
---

# Frames & polysemy

Mahalath treats **polysemy as first-class**. A single human term routinely holds
several co-equal [meanings](lexicon-of-meanings.md), each keyed by the **frame** it
speaks within: a *field* is one thing to a physicist, another to a farmer, another
to a database designer — three distinct meanings, not one fuzzy one.

So every definition is **frame-tagged**, and a term resolves to a *set* of
`(MPL label, frame)` meanings rather than a single entry. This is why
[retrieval](../modules/retrieval.md) returns every codified meaning with its frame
and provenance, and a consumer cites the specific `(label, frame)` it relied on.
Preserving frames rather than collapsing them is the whole point — it is also why
[lexicons are per-language](languages-and-mappings.md), since *how* a language
deploys a term (its illocution) is part of the term's meaning.
