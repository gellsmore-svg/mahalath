---
type: Concept
title: Provenance & staleness
description: Every meaning carries a full audit trail, and references between meanings are extracted into a reverse index; when an upstream entry changes, a staleness cascade flags dependents for audit/redefine, so the ontology self-heals rather than silently drifting.
resource: https://github.com/gellsmore-svg/mahalath/blob/main/src/mahalath/staleness.py
tags: [mahalath, provenance, staleness, self-healing, audit]
timestamp: 2026-06-19T00:00:00Z
---

# Provenance & staleness

Mahalath keeps the [lexicon](lexicon-of-meanings.md) **honest over time**:

- **Provenance** — every meaning records where its evidence came from (SHA-256
  deduped, archived sources) and a full **audit trail** of how its definition and
  placement were reached.
- **Reference graph** — definitions reference other meanings; these references are
  extracted into a **reverse index** (`staleness.py`), so the system knows what
  depends on what.
- **Staleness cascade** — when an upstream entry changes (a redefinition, a
  [hierarchy](hierarchy.md) move), dependents are flagged **stale** and queued for
  **audit/redefine**. The ontology **self-heals** rather than silently drifting out
  of consistency.

This is the maintenance backbone that lets Mahalath run unattended: ingest changes
propagate through the reference graph instead of leaving stale definitions behind.
Driven by the [scheduler](../modules/maintenance.md); results surface as
[operator proposals](operator-review.md).
