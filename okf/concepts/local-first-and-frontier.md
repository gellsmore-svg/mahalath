---
type: Concept
title: Local-first & frontier review
description: Mahalath runs local-first on MongoDB + Ollama; a frontier model (Anthropic Claude) is invoked only as an optional review pass over the items the local model is not confident about, keeping cost and dependency low while raising quality where it matters.
resource: https://github.com/gellsmore-svg/mahalath/blob/main/src/mahalath/frontier.py
tags: [mahalath, local-first, frontier, ollama, claude]
timestamp: 2026-06-19T00:00:00Z
---

# Local-first & frontier review

Mahalath is **local-first**: storage is MongoDB and the default models are local
via Ollama, so ingestion, [extraction](../modules/pipeline.md), and
[debate](debate-and-consensus.md) run with no cloud dependency.

A **frontier model** (Anthropic Claude) is an **optional review pass** (`frontier.py`)
applied only to the `pending_review` queue — the items the local model is *not
confident about*. This is the deliberate cost/quality split: the bulk of the work
is local and free; frontier judgement is spent only where confidence is low. It
composes with [operator review](operator-review.md) (the human gate) and the
[scheduler](../modules/maintenance.md) (which runs the review pass autonomously).
Frontier use is configurable and off unless an API key is provided.
