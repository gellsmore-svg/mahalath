# Changelog

All notable changes to Mahalath are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.6.0] — 2026-08-11

First operation of ADR-036 against the live corpus, and what it exposed.

### Added
- **`runtime.relatedness_model`** — the model that judges document relatedness,
  separate from the debate default (`None` → `runtime.model`), matching the
  existing per-role override pattern. Threaded through both call sites: the
  `link-documents` CLI (explicit `--model` still wins) and the ingest hook —
  without the second, turning the knob would have fixed the manual command and
  left automatic linking on the fast model.

  Pinning it matters. Judging "same work vs merely the same topic" requires
  reading both documents rather than reasoning from their titles, and a small
  model does not. Measured on the live corpus: `gemma2:2b` called an English
  ontology and an unrelated German pilot a `translation` at confidence 8.0,
  inferring it from the word "Edition"; `mistral-small` returned not-related at
  9.5 on the identical prompt. Both were confident, so raising
  `--min-confidence` is no substitute for a capable judge.
- `link_related_documents` and `relatedness_model` documented in
  `config.example.yaml` (neither was discoverable before).

### Fixed
- **Relatedness refuses to judge a document whose text cannot be read.**
  `_sample()` returned an empty string when the archived file was missing, and
  the judge was then handed a *title* to compare against a full document —
  producing a confident verdict from no evidence and recording it as a link.
  Found on the live corpus, where one document's archive had been deleted while
  its database record remained. Now raises for the incoming document and skips
  unreadable candidates without spending a model call.

### Notes
- Requires **hoglah >= 0.10.0** on the *worker daemon*, not just the client. A
  0.9.0 daemon cannot deserialise a request carrying `depends_on`,
  `idempotency_key`, `retry_policy` or `run_at` and fails every job with
  Hoglah's generic `"Unexpected worker error"`. Symptom: jobs enqueue, run and
  fail with no usable reason. Fix: upgrade and restart the daemon.

## [1.5.0] — 2026-08-11

### Added
- **Conversation history for every prose layer (ADR-034).** Every model call
  that contributes prose to a term now writes a `decision_log` row plus its
  `agent_exchanges`, linked from the definition and readable. `detailed_text`
  previously recorded *nothing* — the prompt and response were lost the moment
  the call returned — and now carries `detailed_model_used`,
  `detailed_created_at` and `detailed_decision_log_id` distinct from the
  debate's. New `mahalath show-decision <id|MPL-label>` (with `--verbose`,
  `--layer`, `--json`), a "How this term was arrived at" table on the entry
  page, and a `/decisions/{id}` view with collapsible prompts. Prose is not
  stored if its audit write fails: better no exposition than one with no record
  of where it came from. Expansions use the non-debate outcome `elaborated` and
  are excluded from debate statistics so acceptance rates stay about debates.
- **Operator review gated on confidence after recursion (ADR-037).**
  `/undecided` is now a review queue rather than a list of everything pending:
  terms surface once attempted `REVIEW_ESCALATION_THRESHOLD` (2) times and
  still below `runtime.confidence_threshold`, or immediately for `conflict` and
  `moderator_block`, which re-debate does not resolve. Undebated `proposed_term`
  entries never surface. The page reports how many items are still being
  retried, so an empty queue reads as "nothing needs you" rather than "nothing
  is happening". Accept/reject actions on the page and via
  `mahalath needs-review` / `accept-undecided` / `reject-undecided`, writing to
  a new `operator_decisions` audit collection. An operator accept mints its own
  `decision_log_id` and names the debate it overrode.
- **Related-document linking and term correspondence (ADR-036).** New
  `mahalath link-documents` asks the model whether an incoming document is
  related to one already processed (revision, translation, excerpt, shared
  material) and records the link; `--correspond` matches terms across the pair.
  `mahalath compare-documents <link-id>` reports shared terms, terms unique to
  each side, and definitions that differ — the first way to answer "did that
  model or process change improve the output?". **Not deduplication:** the
  incoming document is processed in full and the original's terms are never
  modified. Opt-in at ingest via `runtime.link_related_documents`.
- **Design (ADR-033):** scholarly layer + same-document lesson memory —
  [`docs/scholarly-layer.md`](docs/scholarly-layer.md). Three prose layers per
  sense; debate transcripts remain ground truth. The lesson-memory half is
  **deferred to DQ-015**; the transcript half shipped as ADR-034.

### Fixed
- **Redefine-generated expositions had no corpus text (ADR-035).** The accept
  path passed a source snippet; the redefine path did not, so a definition
  rewritten overnight was asked to describe corpus usage with none in front of
  it and the model invented an example. New `source_snippet_for_entry` supplies
  a passage, preferring the triggering document.
- **The triggering document is recorded, not `source_document_ids[0]`.** 33 of
  119 live entries carry several sources, so the first one is a guess;
  `redefine_stale_entry` and `redebate_entry` now accept
  `triggering_document_id`, and the multi-source fallback is logged. This is
  the ADR-033 prerequisite: without it, same-document scoping would filter
  correctly over the wrong material.
- **`backfill-detailed --max-items` bounds attempts, not successes.** With a
  failing model it previously walked the entire collection regardless of the
  limit (3 requested → 20 attempted in a 20-entry probe). Now matches the
  `backfill-intents` / `backfill-contexts` pattern. Failures also report the
  adapter's own message instead of a fixed `"generation failed"`.

## [1.4.0] — 2026-08-10

### Added
- **Detailed definition expositions:** each `DefinitionVersion` may carry
  `detailed_text` — a longer description of the *same* sense as the debated
  short `text` (not a second frame). Generated best-effort after debate accept
  and REM redefine when `runtime.generate_detailed_definitions` is true
  (default). Surfaces in retrieval `Meaning.detailed_description`, glossary
  MD/JSON, chat context, and the web entry page.
- `mahalath backfill-detailed [--apply] [--overwrite] [--max-items N]` to fill
  or regenerate detailed text on existing corpus definitions.
- Module `mahalath.detailed` (prompt, parse, enrich, backfill).

## [1.3.0] — 2026-08-08

Review action for `docs/review-2026-08-08.md` (1.2.0 baseline).

### Fixed
- **H1/H2 / F1**: REM redefine no longer appends a duplicate definition or
  cascades staleness when the model re-derives text already present in the
  same frame (whitespace-normalised). Stale is still cleared.
- **H3 / F3**: `rem_redefine` writes a `decision_log` + `agent_exchanges` row
  and links `decision_log_id` on the appended definition (audit trail for
  meaning changes).
- **F4**: argparse subcommand errors print that subcommand's usage instead of
  the top-level command list (`_SubcommandParser`).
- **F5**: `list-ontology` accepts `--limit` and `--skip`.
- **F6**: default `model_adapter` / `chat_adapter` is `ollama_http` (no
  `ollama` binary on PATH required).

### Added
- **F2**: `dedupe_identical_frame_definitions` + CLI
  `mahalath dedupe-definitions [--apply]` for one-off cleanup of exact
  same-frame duplicate definitions.
- Tests: no-op redefine (no append, no cascade), decision_log link on
  redefine, frame dedupe dry-run/apply.

### Changed
- **M1**: README clarifies that `consensus_score` is populated by the debate
  path only; REM redefine and operator definitions leave it null by design.
- **M4**: PEP 639 license metadata (`license = "Apache-2.0"`,
  `license-files = ["LICENSE"]`).

## [1.2.0] — 2026-08-07

### Added
- `mahalath.deborah` — novel-concept detection for Deborah substrate slice
- Manifest capability `detect_novel`

### Added
- **Family trace spine emission** (`galeed:` config section, `MAHALATH_GALEED_*`
  env, `galeed` extra): `document.ingested`, `debate.completed`, and
  `proposal.accepted/rejected/rolled_back` events — best-effort, off by default.

### Changed
- **Web UI restyle**: token-based CSS with automatic dark mode
  (`prefers-color-scheme`), sticky nav with active page, clickable dashboard
  cards, inline confidence meters, chip filters.

### Fixed
- Witness lazy Mongo init thread race that could silently drop events.

### Added
- **`ollama_http` adapter** (`model_adapter = "ollama_http"`): talks to Ollama purely
  over HTTP (`/api/generate` + `/api/embed`), no `ollama` binary on PATH required —
  the portable counterpart to `ollama_cli`. Validated live (generate + 1024-dim
  embeddings).
- `mahalath migrate` — a consolidated, ordered, **idempotent** schema-migration
  command with a `schema_migrations` ledger, replacing the scattered
  `backfill-*` one-shots (which remain as legacy aliases). Supports `--status`
  and `--dry-run`.

## [1.1.0] - 2026-06-22

Declared in `pyproject` earlier but first tagged here. All additions are
backward-compatible.

### Added
- **Optional Hoglah adapter** (`model_adapter = "hoglah"`): route generation +
  embedding calls through a Hoglah queue daemon for durable, serialized execution.
- **Hoglah messaging transports** (Kafka / RabbitMQ / Redis) for the Hoglah path.
- CI: pytest workflow with a MongoDB service.
- OKF (Open Knowledge Format) knowledge bundle.

### Fixed
- `ollama_executable` now defaults to a PATH-resolved `ollama` instead of a
  hardcoded WSL path; a fresh install is portable.
- `__version__` is now derived from package metadata, ending the
  `pyproject` (1.1.0) vs `__init__` (0.0.1) version mismatch.

### Changed
- `load_config` applies env overrides (`OLLAMA_BASE_URL`, `OLLAMA_EXECUTABLE`,
  `MAHALATH_MONGO_URI/DB`) on top of YAML, even with no config file.
- REM scheduler test made CI-safe (mock adapter); internal/collaboration
  references cleaned for the public repo.

## [1.0.0] - 2026-06-13
### Added
- Initial public release: multi-agent MPL ontology builder with REM-style
  refinement (MongoDB store, ontology entries + definition contexts/frames,
  mappings, glossary export, CLI + optional FastAPI web surface).

[Unreleased]: https://github.com/gellsmore-svg/mahalath/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/gellsmore-svg/mahalath/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/gellsmore-svg/mahalath/releases/tag/v1.0.0
