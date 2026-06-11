"""Glossary export: turn the ontology into consumable Markdown / JSON.

Stage 1 produces ontology_entries records; Stage 2 hierarchy review
attaches them in a tree; the rest of the world wants to read the
result. This module flattens the live `mahalath_*` database into two
shareable formats:

  Markdown: human-facing, ordered by MPL label, one section per entry,
            children listed under each parent, aliases enumerated.
            Suitable for committing to a docs site or sharing with a
            reader.

  JSON:     machine-facing, full schema fidelity (definitions with
            model + timestamp, source_document_ids, decision_log_id,
            children, aliases). Suitable for downstream tooling.

No agent calls are made; this is a pure read + render of the current
database state. Re-run after every process-document if you want a
fresh artifact.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pymongo.database import Database

from mahalath.db.models import OntologyEntry
from mahalath.db.repositories import (
    DefinitionContextRepository,
    OntologyEntryRepository,
    OntologyTreeRepository,
)


DEFAULT_GLOSSARY_BASENAME = "glossary"
DEFAULT_AUTO_FORMATS: tuple[str, ...] = ("md", "json")


@dataclass(frozen=True)
class ExportResult:
    format: str          # "md" or "json"
    entry_count: int
    output: str
    written_to: Path | None = None


@dataclass
class _EntryWithChildren:
    entry: OntologyEntry
    children: list[str]


def export_markdown(
    db: Database,
    *,
    out_path: Path | None = None,
    database_name: str = "mahalath",
) -> ExportResult:
    entries = _gather_entries(db)
    body = _render_markdown(entries, database_name, _context_names(db))
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(body, encoding="utf-8")
    return ExportResult(
        format="md",
        entry_count=len(entries),
        output=body,
        written_to=out_path,
    )


def refresh_glossary(
    config,
    db: Database,
    *,
    formats: tuple[str, ...] = DEFAULT_AUTO_FORMATS,
    basename: str = DEFAULT_GLOSSARY_BASENAME,
    project_root: Path | None = None,
) -> dict[str, ExportResult]:
    """Write glossary artifacts to `paths.ontology/glossary.{md,json}`.

    Idempotent: same ontology produces the same bytes. Called from
    process-input, process-document, and the scheduler's REM job
    after operations that may have changed the ontology. The
    operator can `git diff ontology/` after every overnight run.

    `config` may be any object with `.paths.ontology` (a Path or str)
    and `.mongo.database` (str) — typed as AppConfig in practice.
    """
    root = project_root or Path.cwd()
    out_dir = Path(config.paths.ontology)
    if not out_dir.is_absolute():
        out_dir = root / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, ExportResult] = {}
    for fmt in formats:
        if fmt == "md":
            results["md"] = export_markdown(
                db,
                out_path=out_dir / f"{basename}.md",
                database_name=config.mongo.database,
            )
        elif fmt == "json":
            results["json"] = export_json(
                db,
                out_path=out_dir / f"{basename}.json",
                database_name=config.mongo.database,
            )
        else:
            raise ValueError(f"unknown glossary format: {fmt!r}")
    return results


def export_json(
    db: Database,
    *,
    out_path: Path | None = None,
    database_name: str = "mahalath",
) -> ExportResult:
    entries = _gather_entries(db)
    payload = _build_json_payload(entries, database_name, _context_names(db))
    body = json.dumps(payload, indent=2, default=_json_default)
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(body, encoding="utf-8")
    return ExportResult(
        format="json",
        entry_count=len(entries),
        output=body,
        written_to=out_path,
    )


# --- Gathering -------------------------------------------------------------


def _context_names(db: Database) -> dict[str, str]:
    """{context_id → readable frame name} for definition grouping."""
    return {
        c.context_id: c.name for c in DefinitionContextRepository(db).all()
    }


def _gather_entries(db: Database) -> list[_EntryWithChildren]:
    entry_repo = OntologyEntryRepository(db)
    tree_repo = OntologyTreeRepository(db)
    result: list[_EntryWithChildren] = []
    for label in sorted(entry_repo.all_labels()):
        entry = entry_repo.get(label)
        if entry is None:
            continue
        children = sorted(tree_repo.children_of(label))
        result.append(_EntryWithChildren(entry=entry, children=children))
    return result


# --- Markdown --------------------------------------------------------------


def _render_markdown(
    entries: list[_EntryWithChildren],
    database_name: str,
    ctx_names: dict[str, str],
) -> str:
    generated_at = datetime.now(timezone.utc).isoformat()
    n = len(entries)
    n_roots = sum(1 for ec in entries if ec.entry.parent_label is None)
    n_derived = n - n_roots

    lines = [
        "# Mahalath Ontology Glossary",
        "",
        f"Generated {generated_at} from `{database_name}`.",
        "",
        f"**{n}** entries · **{n_roots}** top-level · "
        f"**{n_derived}** with parent",
        "",
        "---",
        "",
    ]

    if not entries:
        lines.append("_(ontology is empty)_")
        lines.append("")
        return "\n".join(lines)

    for ec in entries:
        e = ec.entry
        parent_display = (
            f"`{e.parent_label}`" if e.parent_label else "_top-level_"
        )
        lines.append(f"## {e.mpl_label} — {e.canonical_term}")
        lines.append("")
        lines.append(
            f"**Confidence:** {e.confidence:.1f} · "
            f"**Status:** {e.status} · "
            f"**Parent:** {parent_display}"
        )
        lines.append("")

        if e.definitions:
            # Group by frame (context) when any definition carries one,
            # mirroring the web UI's polysemy rendering. A frameless
            # entry renders flat exactly as before.
            if any(d.context_id for d in e.definitions):
                groups: dict[str | None, list] = {}
                order: list[str | None] = []
                for d in e.definitions:
                    if d.context_id not in groups:
                        groups[d.context_id] = []
                        order.append(d.context_id)
                    groups[d.context_id].append(d)
                # Untagged definitions render last.
                if None in groups:
                    order.remove(None)
                    order.append(None)
                for cid in order:
                    if cid is None:
                        lines.append("**Frame:** _(untagged)_")
                    else:
                        frame = ctx_names.get(cid, cid)
                        lines.append(f"**Frame:** _{frame}_")
                    lines.append("")
                    for definition in groups[cid]:
                        lines.extend(_render_definition(definition))
            else:
                for definition in e.definitions:
                    lines.extend(_render_definition(definition))

        if e.aliases:
            lines.append(
                "**Aliases:** " + ", ".join(f"_{a}_" for a in e.aliases)
            )
            lines.append("")

        if ec.children:
            lines.append("**Children:**")
            for child in ec.children:
                lines.append(f"- `{child}`")
            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def _render_definition(definition: Any) -> list[str]:
    lines = [f"> {definition.text}"]
    attribution_parts = []
    if definition.model_used:
        attribution_parts.append(f"from `{definition.model_used}`")
    if definition.created_at:
        attribution_parts.append(_iso(definition.created_at))
    if attribution_parts:
        lines.append("")
        lines.append(f"_— {', '.join(attribution_parts)}_")
    lines.append("")
    return lines


# --- JSON ------------------------------------------------------------------


def _build_json_payload(
    entries: list[_EntryWithChildren],
    database_name: str,
    ctx_names: dict[str, str],
) -> dict[str, Any]:
    return {
        "schema": "mahalath.glossary.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": database_name,
        "count": len(entries),
        "entries": [
            {
                "mpl_label": ec.entry.mpl_label,
                "canonical_term": ec.entry.canonical_term,
                "aliases": list(ec.entry.aliases),
                "parent_label": ec.entry.parent_label,
                "children": list(ec.children),
                "confidence": ec.entry.confidence,
                "status": ec.entry.status,
                "definitions": [
                    {
                        "text": d.text,
                        "language": d.language,
                        "model_used": d.model_used,
                        "decision_log_id": d.decision_log_id,
                        # The frame this definition speaks within
                        # (polysemy, S2.22+). Additive — schema stays v1.
                        "context_id": d.context_id,
                        "context_name": (
                            ctx_names.get(d.context_id)
                            if d.context_id else None
                        ),
                        "created_at": d.created_at,
                    }
                    for d in ec.entry.definitions
                ],
                "source_document_ids": list(ec.entry.source_document_ids),
                "decision_log_id": ec.entry.decision_log_id,
                "created_at": ec.entry.created_at,
                "updated_at": ec.entry.updated_at,
            }
            for ec in entries
        ],
    }


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
