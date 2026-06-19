---
type: Concept
title: Operator review
description: Significant changes — new entries, redefinitions, hierarchy moves, mappings — surface as action proposals in operator-reviewable queues; a human operator approves, rejects, or defers, so autonomous operation stays accountable rather than silently rewriting the lexicon.
resource: https://github.com/gellsmore-svg/mahalath/blob/main/src/mahalath/proposals.py
tags: [mahalath, operator, proposals, review, governance]
timestamp: 2026-06-19T00:00:00Z
---

# Operator review

Mahalath runs autonomously but stays **accountable**. Consequential changes are not
applied silently; they surface as **action proposals** in operator-reviewable
queues (`proposals.py`):

- new ontology entries and redefinitions from [debate](debate-and-consensus.md);
- [hierarchy](hierarchy.md) placements/moves;
- [staleness](provenance-and-staleness.md)-triggered audit/redefine actions;
- cross-language [mapping](languages-and-mappings.md) assertions.

The **operator** (the human running the system) approves, rejects, or defers each
proposal. This keeps the lexicon trustworthy: the system proposes, a human
disposes, and every decision joins the [audit trail](provenance-and-staleness.md).
"Operator" is Mahalath's product term for that human-in-the-loop role.
