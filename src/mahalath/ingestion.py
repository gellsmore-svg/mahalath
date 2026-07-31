"""Document ingestion: read, hash, dedupe, archive, record, log.

Stage 1 ingests one Markdown document at a time. Per ADR-015 the source
file is preserved by copying to `paths.processed/` (a logical archive
inside the working directory) before the DocumentRecord is written.
Per ADR-016 duplicate detection uses SHA-256 over the raw bytes — same
content under a different name still counts as a duplicate.

Side effects in order:

1. Read source bytes, compute SHA-256.
2. Query documents collection for an existing record with that checksum.
3. If duplicate: return IngestionResult(duplicate=True, document=existing).
4. Else: choose archive path (paths.processed/<name>, with suffix if name
   collides), copy source to archive, verify archived checksum.
5. Build DocumentRecord (title from first heading or filename stem),
   insert into documents.
6. Emit a Markdown activity log at paths.logs/ingest-<document_id>.md.

The function is pure data-flow once given an open Database; the CLI
layer wraps it with config loading, error formatting, and exit codes.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from pymongo.database import Database

from mahalath.config import AppConfig
from mahalath.db.models import DocumentRecord
from mahalath.db.repositories import DocumentRepository
from mahalath.tracing import DOCUMENT_INGESTED, get_witness


class IngestionError(Exception):
    """Raised when ingestion fails for a reason that should surface to the operator."""


@dataclass
class IngestionResult:
    duplicate: bool
    document: DocumentRecord
    activity_log_path: Path | None = None


_HEADING_PATTERN = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def _extract_title(text: str, fallback: str) -> str:
    match = _HEADING_PATTERN.search(text)
    if match:
        return match.group(1).strip()
    return fallback


def _checksum_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _choose_archive_path(processed_dir: Path, source_name: str, checksum: str) -> Path:
    """Pick a non-colliding archive path inside processed_dir.

    First choice is `processed_dir / source_name`. If that exists, fall
    back to `<stem>__<checksum8><suffix>` to keep the human-readable
    name visible while guaranteeing uniqueness.
    """
    primary = processed_dir / source_name
    if not primary.exists():
        return primary
    stem = Path(source_name).stem
    suffix = Path(source_name).suffix
    return processed_dir / f"{stem}__{checksum[:8]}{suffix}"


def ingest_one(
    source_path: Path,
    config: AppConfig,
    db: Database,
    *,
    project_root: Path | None = None,
    style_overlay_path: str | None = None,
    language: str = "en",
) -> IngestionResult:
    if not source_path.exists():
        raise IngestionError(f"Source file not found: {source_path}")
    if not source_path.is_file():
        raise IngestionError(f"Source path is not a regular file: {source_path}")

    root = project_root or Path.cwd()
    processed_dir = root / config.paths.processed
    logs_dir = root / config.paths.logs
    processed_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    raw_bytes = source_path.read_bytes()
    checksum = _checksum_bytes(raw_bytes)

    docs = DocumentRepository(db)
    existing = docs.find_by_checksum(checksum)
    if existing is not None:
        return IngestionResult(duplicate=True, document=existing)

    text = raw_bytes.decode("utf-8", errors="replace")
    title = _extract_title(text, fallback=source_path.stem)

    archive_path = _choose_archive_path(processed_dir, source_path.name, checksum)
    shutil.copy2(source_path, archive_path)

    archived_checksum = _checksum_bytes(archive_path.read_bytes())
    if archived_checksum != checksum:
        archive_path.unlink(missing_ok=True)
        raise IngestionError(
            f"Archive copy checksum mismatch for {source_path}; aborted."
        )

    record = DocumentRecord(
        source_path=str(source_path),
        archive_path=str(archive_path.relative_to(root)) if archive_path.is_relative_to(root) else str(archive_path),
        checksum_sha256=checksum,
        title=title,
        byte_size=len(raw_bytes),
        char_count=len(text),
        style_overlay_path=style_overlay_path,
        language=language,
    )
    docs.insert(record)

    log_path = _write_activity_log(logs_dir, record, source_path, archive_path)

    get_witness().emit(
        DOCUMENT_INGESTED,
        trace_id=record.document_id,
        summary=f"ingested '{record.title}' ({record.char_count} chars)",
        document_id=record.document_id,
        title=record.title,
        char_count=record.char_count,
    )

    return IngestionResult(
        duplicate=False,
        document=record,
        activity_log_path=log_path,
    )


def _write_activity_log(
    logs_dir: Path,
    record: DocumentRecord,
    source_path: Path,
    archive_path: Path,
) -> Path:
    log_path = logs_dir / f"ingest-{record.document_id}.md"
    body = f"""# Ingestion log: {record.title}

- document_id: `{record.document_id}`
- source_path: `{source_path}`
- archive_path: `{archive_path}`
- checksum_sha256: `{record.checksum_sha256}`
- byte_size: {record.byte_size}
- char_count: {record.char_count}
- ingested_at: {record.ingested_at.isoformat()}

## Status

Accepted (new document). No debate has run yet.

## Next steps

- Stage 1.4: extract candidate terms from this document.
- Stage 1.5: run debate loop on each candidate term.
"""
    log_path.write_text(body, encoding="utf-8")
    return log_path
