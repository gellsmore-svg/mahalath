# Changelog

All notable changes to Mahalath are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
