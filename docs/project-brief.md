# Mahalath Project Brief

Last updated: 2026-06-06

## Purpose

Mahalath (MPL) is a self-sustaining, perpetual, multi-agent reasoning
engine that builds and continuously refines a precise internal language
and ontology for AI-to-AI reasoning, with on-demand translation back to
natural human languages.

The system operates as a memory-light, ontology-heavy counterpart to a
memory retrieval layer like Mnemosyne. Where a retrieval system answers
"what do we know that's relevant to this question?", Mahalath answers
"what do the words we use actually mean, and where do they need to be
split?"

## Core Goals

- Reduce linguistic ambiguity in AI reasoning by maintaining a low-noise
  internal vocabulary (MPL) with explicit polysemy handling.
- Continuously refine that vocabulary by debating candidate terms across
  multiple LLM agents, splitting meanings into contradistinct variants
  when nuances surface, and recording the decision trail.
- Operate autonomously: ingest from a watched folder, debate up to a
  configurable iteration cap, store outcomes, queue ambiguous cases for
  human review, and run REM-style overnight batches without supervision.
- Treat MPL as a precision tool, not a closed system. Glossary export
  and natural-language ↔ MPL round-tripping are required outputs.

## Non-Goals (initial version)

- Full graph visualisation.
- Real-time web UI.
- Cloud execution (local-first only).
- Replacing human review for ambiguous cases. Low-confidence outcomes go
  to an undecided queue, not auto-accept.
- Becoming a memory retrieval engine. Mnemosyne fills that role; Mahalath
  feeds it precision, not the other way round.

## Primary Users

Initial user: the local operator, working with the AMS / Relational
Substrate corpus as seed material. The operator wants to use Mahalath
both directly (as a precision-checking tool for their writing) and
indirectly (as an upstream refinement layer for AI tools that consume
the corpus).

Likely future users:

- writers maintaining technical vocabularies that drift between editions
  of long-form work;
- researchers managing evolving subject-specific terminology;
- developers building AI tools that need stable inter-agent
  communication;
- local-first AI users who want inspectable, auditable concept evolution.

## First Useful Outcome

The first useful system should ingest one Markdown document, extract
candidate terms, run a single multi-agent debate cycle on one of them,
write the outcome to MongoDB with provenance and confidence, and emit a
plain-text activity log of the decision. This proves the ingestion →
agent loop → ontology write path end to end on real source material.

This corresponds to Stage 1 in the build roadmap (to be written in the
next chunk after chunk 4 scaffolding is in place).

## Conceptual Relationship to Mnemosyne

Mahalath and Mnemosyne are sibling systems sharing operating philosophy
(local-first, MongoDB, adapter boundary for swappable models, REM-style
async consolidation, multi-agent with bounded iteration,
undecided/review queue, watched ingest folder) but with different
*purposes*.

| Concern | Mnemosyne | Mahalath |
|---|---|---|
| Question answered | Given a query, find relevant content | Given content, refine precise meanings |
| Primary write target | Chunk nodes + edges | Ontology entries (MPL terms) + relations |
| Memory shape | Hierarchical document → tree → chunks | Hybrid flat dictionary + hierarchical taxonomy |
| Agent role | Memory-agent (retrieval) + final answer | PrecisionCritic + SynthesisExplorer + optional Moderator |
| Review queue | Semantic-edge candidates | Undecided terms / contested splits |
| Source attitude | Source preserved; chunks addressable | Source preserved; terms emergent |

The two systems should remain runtime-independent. Cross-pollination
happens through code-reading, not imports. If a shared library makes
sense later (Ollama adapter is the obvious candidate), extract it as a
deliberate decision.

## Seed Corpus

The operator's AMS / Relational Substrate book series, currently
maintained in `~/RS-claude/`:

- `rs-cbo-bachelors-v2.md`, `rs-cbo-alevel-v2.md`, `rs-cbo-gcse-v2.md`,
  `rs-cbo-beginner-v1.md` — the four-edition Coherent Biblical Ontology
  scripture-first series.
- `rs-master-book-v1.md` (*A Coherent World*) and `rs-master-beginner-v1.md`
  (*Why the World Makes Sense*) — the general-volume series.
- `rs-technical-v1.md` (*The Relational Substrate*) — the technical
  ontology volume.

This corpus has known polysemy (e.g. "substrate" in the physics sense vs
in other senses, "alignment" in the ontological sense vs in moral
sense), which makes it a strong test bench for the contradistinct-split
mechanism that section 3.3 of the requirements calls for.

Mahalath should ingest these as source Markdown directly, not via
Mnemosyne. Both systems can hold their own representations of the same
source files without conflict.
