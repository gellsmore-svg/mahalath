---
type: Module
title: Frontier & config
description: The optional frontier-model review pass over low-confidence items, plus configuration, the style overlay, and filesystem paths.
resource: https://github.com/gellsmore-svg/mahalath/blob/main/src/mahalath/frontier.py
tags: [mahalath, frontier, config, style]
timestamp: 2026-06-19T00:00:00Z
---

# Frontier & config

- **`frontier.py`** — the optional **[frontier-model review](../concepts/local-first-and-frontier.md)**
  pass (Anthropic Claude) over the `pending_review` queue: invoked only on items the
  local model is not confident about.
- **`config.py`** — runtime configuration (loaded from `config.yaml`): model
  selection, MongoDB, Ollama, frontier API settings, schedules, thresholds.
- **`style.py`** — the style overlay (definitional phrasing/voice conventions
  applied to generated definitions; see `docs/style-overlay.md`).
- **`paths.py`** — filesystem layout (`input/`, `processed/`, `ontology/`,
  `undecided/`, `logs/`).

These support the [pipeline](pipeline.md), [maintenance](maintenance.md), and
[retrieval](retrieval.md) rather than being part of the lexicon itself.
