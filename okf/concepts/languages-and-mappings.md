---
type: Concept
title: Languages & mappings
description: A lexicon belongs to one language; languages are discrete peers, never derived from each other, each built from its own evidence with its own tree. Cross-language relationships, when built, are explicit, weighted, debated mapping assertions — never translation at ingestion.
resource: https://github.com/gellsmore-svg/mahalath/blob/main/src/mahalath/mappings.py
tags: [mahalath, languages, mappings, translation, adr-029]
timestamp: 2026-06-19T00:00:00Z
---

# Languages & mappings

A [lexicon](lexicon-of-meanings.md) belongs to **one language** (the live one is
English). Languages are **discrete peers**, never derived from each other: a German
lexicon would be built from German evidence with its own tree, because terms across
languages are rarely like-for-like — which is the very reason the system exists.
Labels are opaque and globally sequenced, but each addresses a meaning *within its
language's lexicon*; there is **no language-independent "concept" node** above them.

**Cross-language relationships are mappings, not translations.** When built
(`mappings.py`, ADR-028–030, phased on the backlog) they are **explicit, weighted,
debated mapping assertions** between meanings — supporting translation
drafting/review and cross-language comparison of *illocution* (how each language
deploys a term, which is part of its meaning). Translation never happens at
ingestion; evidence enters in its own language and is mapped deliberately, under
[operator review](operator-review.md).
