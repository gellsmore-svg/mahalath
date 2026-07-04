# Contributing to Mahalath

Thanks for your interest in Mahalath. This guide covers local development.

## Development setup

```bash
git clone https://github.com/gellsmore-svg/mahalath
cd mahalath
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Configuration lives in `config.yaml` (local, gitignored), with
`config.example.yaml` as the committed template. The runtime folders `input/`,
`processed/`, `ontology/`, `logs/`, and `undecided/` are gitignored (the folders
are committed via `.gitkeep`, their contents are not).

## Running tests

Integration tests need a MongoDB server on `localhost:27017` (for example
`docker run -d -p 27017:27017 mongo:8.0`).

```bash
pytest
```

## Code conventions

- Python 3.11+
- Source under `src/mahalath/`, tests under `tests/`
- Decisions are recorded (append-only) in `docs/architecture-decisions.md`

## Standing principles

These follow from `docs/requirements-v0.1.md` and apply by default:

- **Review-based writes.** Semantic/ontology writes stay review-based; no
  autonomous agent write authority before review queues, audit logs, and
  rollback exist.
- **Source-preserved.** Ingestion never silently rewrites, compresses, or
  summarises source documents — the source is authoritative; derived content is
  regeneratable.
- Please open an issue before changes that alter source preservation, agent
  write authority, the adapter boundary, or any "Out of scope" item in
  `docs/requirements-v0.1.md` §7.

## Reporting issues

Bugs and questions: <https://github.com/gellsmore-svg/mahalath/issues>. Security
issues: please report privately — see [SECURITY.md](SECURITY.md).
