# Contributing to Mahalath

This file is the working convention for collaboration between the operator,
codex, and Claude on the Mahalath repository. It is short on purpose. If
something is unclear, prefer adding to this file over having the same
discussion twice.

## Roles

- **Operator** — the human owner of the project. Makes architectural and
  scope decisions. The codex and Claude both work on the operator's
  behalf.
- **Codex** — runs in the WSL root user and handles `git push` to the
  remote. Has its own credential setup not visible to Claude. Writes
  files as `root:root`.
- **Claude** — runs in the WSL cello user. Commits locally; codex pushes.
  Writes files as `cello:cello`. Cannot push to GitHub from this
  environment.

## Restart-doc discipline

Two files together carry project state:

- `.restart.md` — *canonical current state*. Status, next step, current
  files. Kept tight and human-readable. Surgical edits preferred over
  rewrites. Pre-edit snapshots saved as `.restart.md.<context>-<date>.bak`
  before substantial rewrites.
- `.session-log.md` — *how we got there*. Append-only chronological
  narrative. Each entry: `## YYYY-MM-DD [agent]` heading. Older entries
  stay verbatim; corrections go in a new entry rather than rewriting
  history.

Codex and Claude both append to `.session-log.md`. Either may update
`.restart.md` with surgical edits.

## Cadence convention

Multi-step work is broken into named chunks with visible progress between
each. Typical chunk shape:

> "Chunk N — short description. Doing X."
> [tool call(s)]
> "Chunk N done. Next: chunk N+1."

Parallel tool calls are reserved for cases where they genuinely save
context (reading two related files needed together for the same
decision, grep across files for comparison). Unrelated reads and
TaskCreate batches should not be parallelised.

## Standing rules

These follow directly from the requirements doc and from the operator's
working preferences during Mnemosyne development; they apply by default
to Mahalath until contradicted.

- Keep semantic / ontology writes review-based at first. Do not grant
  agent autonomous write authority before review queues, audit logs,
  and rollback exist.
- Keep source-preserved: do not silently rewrite, compress, or summarise
  source documents during ingestion. Source is authoritative; derived
  content is regeneratable.
- Ask before implementing changes that alter source preservation, agent
  write authority, the adapter boundary, or any "Out of scope" item
  named in `docs/requirements-v0.1.md` §7.
- Prefer extending the docs in this repo over carrying decisions in
  chat. If a decision was discussed, it belongs in
  `docs/architecture-decisions.md` or as a new line in `.session-log.md`.

## Code conventions (skeletal — to be filled in during chunk 4)

- Python 3.11+
- Source under `src/mahalath/`
- Tests under `tests/`
- Configuration in `config.yaml` (operator's local), `config.example.yaml`
  (committed)
- Five runtime folders, all gitignored: `input/`, `processed/`,
  `ontology/`, `logs/`, `undecided/`.

## Working with Mnemosyne nearby

Mahalath and Mnemosyne live in sibling directories under
`~/domains/`. They share operating philosophy (local-first, MongoDB,
adapter boundary, REM-style consolidation) but should remain
runtime-independent. Cross-pollination should happen through reading
each other's code, not through imports. If a shared library makes sense
later, extract it deliberately as a separate decision recorded in
`docs/architecture-decisions.md`.
