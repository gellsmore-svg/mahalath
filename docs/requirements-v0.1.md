# Mahalath (MPL) — Requirements Document

> **Historical document** — the frozen v0.1 contract. Where shipped behaviour differs (debate iteration cap, web UI), inline notes mark it; current behaviour lives in the README and config defaults.

**Version:** 0.1
**Status:** working draft
**Date captured:** 2026-06-06
**Source:** operator-supplied initial requirements

This document captures the operator's initial Mahalath requirements
verbatim. Subsequent requirement versions should be filed alongside this
one (`requirements-v0.2.md`, etc.) rather than overwriting it, so the
evolution of the spec stays inspectable.

---

## 1. Project Overview

**Mahalath** (MPL) is a self-sustaining, perpetual, multi-agent AI reasoning engine that builds and continuously refines a highly precise internal language/ontology.

It ingests documents (starting with the user's book as seed), disambiguates meanings, creates contradistinct terms when nuances require it, and maintains a living taxonomy. The goal is maximum precision for AI-to-AI reasoning, with the ability to translate back to natural human languages on demand. The system runs autonomously with human review only when needed.

The name "Mahalath" draws from a rare Old Testament Hebrew name associated with musical resonance and stringed instruments — symbolizing harmony, tuning, and precise structure.

## 2. Core Objectives

- Create and evolve a precise, low-ambiguity internal language (Mahalath / MPL) for AI reasoning.
- Build a dynamic ontology/dictionary of meanings that self-refines over time.
- Minimize linguistic drift and maximize precision in complex topics.
- Support high autonomy, including offline "REM-style" processing.
- Integrate human input (documents) while delegating most decisions to agents.

## 3. Functional Requirements

### 3.1 Ontology Management

- Maintain a **hybrid flat + tree structure**:
  - Flat dictionary for fast lookup by unique label.
  - Hierarchical tree/taxonomy for navigation and inheritance.
- Each entry includes: unique AI-chosen label, definition(s), relations, confidence score, decision log.
- Agents decide label format (hierarchical numeric recommended, e.g. `MPL-001.000.001a`).
- Support label evolution (with no duplication/collision).

### 3.2 Multi-Agent Conversation

- Minimum 2–3 agents of similar capability (e.g., PrecisionCritic for logic/drift detection, SynthesisExplorer for correlations/analogies, optional Moderator).
- Agents converse iteratively on terms extracted from ingested documents.
- Max 25 conversation iterations per term before escalating (configurable).  
  *(shipped default is 50 — `runtime.max_iterations_per_term`; the v0.1 number is historical)*
- If confidence < 8.0, move to undecided queue.

### 3.3 Ingestion & Refinement

- Watch an `input/` folder for new documents.
- Process → archive to `processed/` folder.
- Chunk documents, extract candidate terms, debate splits vs. variations.
- Dynamically split meanings into contradistinct variants when new nuances appear.
- Initial seed: User's book (AMS/RS framework).
- Definitions start in English but can evolve to use internal MPL labels.

### 3.4 Autonomy & Safety

- Run perpetual loops with configurable sleep intervals (support REM-style overnight batch thinking).
- Log all agent reasoning, decisions, and confidence scores.
- Undecided queue for items needing human or more powerful LLM review.
- Periodic self-analysis of decision-making effectiveness.

### 3.5 Translation & Usability

- On-demand generation of human-readable glossaries.
- Support round-trip translation: natural language ↔ Mahalath internal representation.
- Multi-language support as a byproduct.

### 3.6 Multi-Model Support

- Easy swapping and concurrent use of local models (Gemma 4 8-bit, Qwen, Mistral/European variant, etc.).

## 4. Non-Functional Requirements

- **Precision-first**: Core metric is precision and confidence.
- **Autonomy**: High autonomy with safe guardrails.
- **Simplicity**: Prefer straightforward storage (MongoDB recommended + Python interface).
- **Persistence**: All ontology, logs, and queues stored durably.
- **Scalability**: Handle growing vocabulary far beyond human manual capacity.
- **Neutrality**: Minimize emotional/bias loading in internal terms.

## 5. Technical Requirements

- **Language**: Python.
- **Database**: MongoDB (for flexible schema).
- **Storage**: JSON fallback possible.
- **Inference**: Local models via Ollama / llama.cpp / similar.
- **Folder Structure**: `input/`, `processed/`, `ontology/`, `logs/`, `undecided/`.

## 6. Success Criteria

- System produces a growing, navigable Mahalath ontology with high-precision terms.
- Agents successfully split/refine meanings from the seed book and new documents.
- LLM using Mahalath internally shows improved precision before translating back to human language.
- Stable autonomous operation with minimal intervention.
- Useful glossary/translation output for the user.

## 7. Out of Scope (for initial version)

- Full graph visualization.
- Real-time web UI.  
  *(superseded: a full FastAPI web UI + grounded chat shipped in 1.x — this document is the frozen v0.1 contract, not current scope)*
- Cloud execution (local-first).
