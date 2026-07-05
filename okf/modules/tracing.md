---
type: Module
title: tracing
description: The Galeed witness — document.ingested, debate.completed, and proposal.accepted/rejected/rolled_back events onto the family spine, gated by the galeed: config section; best-effort, off by default.
resource: https://github.com/gellsmore-svg/mahalath/blob/main/src/mahalath/tracing.py
tags: [mahalath, module, tracing, galeed]
timestamp: 2026-07-05T00:00:00Z
---

# tracing

With `galeed.enabled` (config section or `MAHALATH_GALEED_*` env — the
`galeed` extra), the pipeline testifies onto the family spine:
`document.ingested` (trace = document id), `debate.completed` (trace =
decision-log id; outcome/iterations/confidence), and
`proposal.accepted/rejected/rolled_back` (trace = proposal id).

A process-wide `Witness` singleton (`get_witness()` / `set_witness()` for
tests) keeps call sites signature-free. The lazy Mongo handle is locked,
resolved-flag-last; every failure is swallowed — tracing never affects the
ontology pipeline. Events land in the database the family trace API reads
(default `mnemosyne_dev`), viewable in Mizpah or `galeed trace`.
