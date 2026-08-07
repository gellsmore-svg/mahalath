# Changelog

All notable changes to Mahalath are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
