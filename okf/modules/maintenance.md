---
type: Module
title: Maintenance (autonomous loop)
description: The self-sustaining upkeep — an APScheduler harness drives nightly REM re-debate of the undecided queue, reference-tracking staleness flagging and cascade, and analysis/actions, so the ontology refines and self-heals without supervision.
resource: https://github.com/gellsmore-svg/mahalath/blob/main/src/mahalath/scheduler.py
tags: [mahalath, maintenance, scheduler, rem, staleness]
timestamp: 2026-06-19T00:00:00Z
---

# Maintenance (autonomous loop)

What makes Mahalath **self-sustaining** — it runs unattended and keeps the
[lexicon](../concepts/lexicon-of-meanings.md) consistent:

- **`scheduler.py`** — an APScheduler harness for autonomous operation: it triggers
  ingestion, the [pipeline](pipeline.md), REM, and the
  [frontier review](frontier-config.md) on a schedule.
- **`rem.py`** — nightly **REM re-review** of the undecided queue: items that
  couldn't reach consensus are [re-debated](../concepts/debate-and-consensus.md)
  with fresh context.
- **`staleness.py`** — reference tracking + **staleness flagging** over the
  ontology graph: builds the reverse index and runs the
  [staleness cascade](../concepts/provenance-and-staleness.md).
- **`actions.py`, `analysis.py`** — the action model behind
  [proposals](../concepts/operator-review.md) and analytical passes over the
  ontology.

Together these are the "drop documents and walk away" property: extraction,
consensus, and self-healing all proceed on their own, surfacing
[proposals](../concepts/operator-review.md) for the operator.
