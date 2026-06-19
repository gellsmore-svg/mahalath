---
type: Concept
title: Debate & consensus
description: Candidate terms are refined by a multi-iteration agent debate — a PrecisionCritic presses for definitional sharpness and a SynthesisExplorer broadens — producing a frame-tagged definition with a per-definition consensus score; undecided items go to a nightly REM re-debate.
resource: https://github.com/gellsmore-svg/mahalath/blob/main/src/mahalath/debate.py
tags: [mahalath, debate, agents, consensus, rem]
timestamp: 2026-06-19T00:00:00Z
---

# Debate & consensus

A candidate term is not accepted on one model's say-so; it is **debated**. The
debate loop (`debate.py`) runs multiple iterations between complementary agents:

- a **PrecisionCritic** that presses for definitional sharpness and exposes
  ambiguity;
- a **SynthesisExplorer** that broadens, reconciles, and proposes synthesis.

The outcome is a [frame-tagged definition](frames-and-polysemy.md) with a
**per-definition consensus score**. Three results are possible:

- **accepted** → an ontology entry, followed by a [hierarchy](hierarchy.md) review
  pass (multi-pass consensus);
- **undecided** → queued for **REM**, a nightly re-debate of the undecided queue
  (`rem.py`);
- low-confidence → optional **[frontier review](local-first-and-frontier.md)** by
  Claude.

This multi-agent, consensus-scored process is what makes the
[lexicon](lexicon-of-meanings.md) definitionally sharp. The loop is driven
autonomously by the [scheduler](../modules/maintenance.md).
