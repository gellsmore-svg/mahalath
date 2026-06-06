# Architecture Decisions

Last updated: 2026-06-06

ADRs are append-only. If a decision is reversed, add a new ADR rather
than editing the old one, and note the supersession in both entries.

## Accepted Decisions

| ID | Decision | Rationale |
|---|---|---|
| ADR-001 | Use Python 3.11+ for core orchestration. | Best fit for local LLM tooling, MongoDB, and the multi-agent debate loop. Matches the operator's existing stack and matches Mnemosyne's choice. |
| ADR-002 | Use MongoDB as primary storage. | Flexible document shape suits the hybrid flat dictionary + hierarchical taxonomy required by §3.1. Same local instance Mnemosyne already uses; logical separation via a distinct database name. |
| ADR-003 | Avoid a dedicated graph database. | MongoDB adjacency-list patterns are sufficient for the ontology graph at the foreseeable scale (vocabulary, not corpus). Matches Mnemosyne ADR-003. |
| ADR-004 | All LLM calls behind an agent adapter. | Allows the local model stack to evolve (Gemma, Qwen, Mistral, future European variants) without rewriting the debate loop. Matches Mnemosyne ADR-004; lifted verbatim because it is load-bearing. |
| ADR-005 | Use Ollama as the initial local model runtime. | The operator already has Ollama configured for Mnemosyne. Avoid a second runtime install at bootstrap time. `llama-cpp-python` remains an optional future adapter. |
| ADR-006 | Hybrid flat + tree ontology layout. | Required by §3.1: flat dictionary for O(1) label lookup, hierarchical taxonomy for navigation and inheritance. Both views point at the same underlying entry records. |
| ADR-007 | Hierarchical numeric labels for ontology entries. | The operator-recommended `MPL-001.000.001a` format in §3.1 is the default. Format is enforceable per ADR-008. |
| ADR-008 | Label format and assignment is an agent decision, not a code constant. | §3.1 requires agents to decide label format. The format is recorded per ontology version and enforced by validation rather than hardcoded. |
| ADR-009 | Maximum 25 agent debate iterations per term before escalation. | §3.2 default. Configurable in `config.yaml`. Iteration cap drives termination and prevents pathological loops. |
| ADR-010 | Confidence threshold 8.0 (out of 10) for accepting an outcome; below threshold goes to the undecided queue. | §3.2 default. Threshold is configurable. Confidence is on a 0.0–10.0 scale to match Mnemosyne's edge confidence scale. |
| ADR-011 | Watched `input/` folder for ingestion; archive to `processed/` after success. | §3.3 requirement. Matches Mnemosyne's watched-folder pattern; same APScheduler-style polling cadence is appropriate. |
| ADR-012 | Five runtime folders, all gitignored: `input/`, `processed/`, `ontology/`, `logs/`, `undecided/`. | §5 requirement. The folders themselves are committed (via `.gitkeep`) but their contents are not. |
| ADR-013 | Use `.restart.md` + `.session-log.md` handoff discipline. | Borrowed from Mnemosyne where it worked well for codex/Claude coordination. `.restart.md` is canonical current state; `.session-log.md` is append-only chronological narrative. |
| ADR-014 | Mahalath remains runtime-independent of Mnemosyne. | The two systems share operating philosophy but serve different purposes. Coupling them at runtime would force one's evolution to drag the other. Code-reading cross-pollination is encouraged; imports across project boundaries are not. |
| ADR-015 | Source material is preserved verbatim. | Mahalath analyses source documents to extract candidate terms; it does not rewrite, summarise, or compress source. Matches Mnemosyne's source-preservation discipline and §3.3 of the requirements. |
| ADR-016 | Reject duplicate source files by SHA-256 checksum at ingest time. | Same content-based duplicate rejection Mnemosyne uses (its ADR-016). Avoids re-processing the same source under a different filename. |
| ADR-017 | Logical separation between Mahalath and Mnemosyne MongoDB databases. | Default database name `mahalath_dev` (configurable). Same Mongo instance; different database. Avoids any collection-name collision and lets each project evolve schemas independently. |

## Open Or Pending Decisions

| ID | Question | Current Lean |
|---|---|---|
| DQ-001 | Exact agent role surface. Three named roles in §3.2 (PrecisionCritic, SynthesisExplorer, optional Moderator) — are these the canonical names, and does the Moderator role default on or off? | Names as given; Moderator default off until iteration cap is approached or first contested split appears. |
| DQ-002 | Should multiple agents run on different local models, or the same model with different system prompts? | Different system prompts on the same model in Stage 1 (cheap, deterministic to compare). Multi-model in a later slice once the adapter boundary proves out. |
| DQ-003 | Initial confidence scoring: who scores it — each agent individually then aggregated, or the Moderator after the debate ends? | Each agent emits its own confidence at each turn; aggregate is the minimum of the two debating agents' final scores (conservative). Reconsider after seeing real outcomes. |
| DQ-004 | Term extraction strategy: deterministic n-gram + frequency, LLM-driven candidate extraction, or both? | LLM-driven candidate extraction during ingestion, with the option to add a deterministic baseline later. Avoids the noise of a frequency-based first pass on philosophical/theological prose. |
| DQ-005 | Contradistinct split mechanic — when agents disagree on whether a term is one meaning or two, how is the split represented? | Two new ontology entries inheriting from a parent disambiguation node; the parent label is retained as a polysemy marker; both children carry context qualifiers. Final shape determined when first split happens. |
| DQ-006 | Definition language — definitions start in English per §3.3 and may evolve to use MPL labels. When does evolution happen? | Eagerly substitute MPL labels for terms already in the ontology when a new definition is written; do not retroactively rewrite definitions. Definition language tracked per entry version. |
| DQ-007 | Glossary export format — Markdown, JSON, both, something else? | Markdown for human consumption, JSON for tool consumption. Both regeneratable from the ontology at any time. |
| DQ-008 | Round-trip translation (natural language ↔ MPL) — built-in command, library function, or agent-driven on demand? | Library function callable from CLI and exposed in the FastAPI surface when that exists. Agent-driven translation is a later option. |
| DQ-009 | REM-style overnight processing cadence — fixed schedule, idle-triggered, or both? | Fixed schedule via APScheduler in Stage 1 (configurable). Idle-triggered if and when the system has live agent traffic to detect idle around. |
| DQ-010 | Multi-model concurrent use — when is concurrency required (specific phases) vs available (configurable)? | Available everywhere via the adapter; default to single-model in Stage 1 to keep iteration shape predictable. Concurrent debate (two different model families arguing) is a later slice. |
| DQ-011 | Should Mahalath ingest the AMS corpus directly from `~/RS-claude/`, or expect operator-controlled file placement into `input/`? | Operator-controlled placement into `input/`. The watched-folder pattern is the architectural commitment in §3.3; an "ingest from path" command is a convenience that should not replace it. |
| DQ-012 | First evaluation criterion — when is Stage 1 "done enough" to move to Stage 2? | One end-to-end ingestion → debate → write → log cycle on one term from the seed corpus, with manual review of the resulting entry quality. Stage 1 done means the loop runs; Stage 2 begins when quality is good enough to trust on a small batch. |

## Notes On Decision Reuse From Mnemosyne

Several decisions above lift directly from Mnemosyne's architecture-decisions
doc (ADR-001 through ADR-004, ADR-013, ADR-015, ADR-016). This is
intentional. Where the operating philosophy is shared and the two systems
both run locally on the same hardware against the same MongoDB instance,
reusing the proven decision saves design effort and gives codex/Claude
a stable shared vocabulary. Any decision lifted from Mnemosyne is named
explicitly so future divergence is visible.
