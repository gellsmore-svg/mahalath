---
type: Concept
title: Hierarchy
description: Accepted meanings are placed in a parent/child hierarchy by a multi-pass consensus review, so the lexicon is a navigable tree of meanings rather than a flat list — supporting precise reasoning about generality, specialisation, and dependency.
resource: https://github.com/gellsmore-svg/mahalath/blob/main/src/mahalath/hierarchy.py
tags: [mahalath, hierarchy, placement, tree]
timestamp: 2026-06-19T00:00:00Z
---

# Hierarchy

The [lexicon](lexicon-of-meanings.md) is a **tree of meanings**, not a flat list.
When a candidate is [accepted](debate-and-consensus.md), a **hierarchy review pass**
(`hierarchy.py`) places it among existing meanings by **multi-pass consensus**
(e.g. three passes), deciding its parent/child relationships.

A navigable hierarchy lets a consumer reason about generality and specialisation —
which meaning is a refinement of which — and underpins the
[reference graph](provenance-and-staleness.md): a meaning's definition may *depend
on* other meanings, so changing a parent or upstream entry can ripple downstream.
Placement is itself a [proposal](operator-review.md) an operator can review, and
re-placement can be revisited in [REM](debate-and-consensus.md).
