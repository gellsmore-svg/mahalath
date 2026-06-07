"""Per-document style overlay resolution tests."""

from __future__ import annotations

from pathlib import Path

from mahalath.config import AppConfig, RuntimeConfig
from mahalath.db.models import DocumentRecord
from mahalath.style import resolve_style_overlay


def _make_document(
    overlay_path: str | None = None, **extra
) -> DocumentRecord:
    defaults = {
        "source_path": "input/x.md",
        "archive_path": "processed/x.md",
        "checksum_sha256": "a" * 64,
        "byte_size": 1,
        "char_count": 1,
        "style_overlay_path": overlay_path,
    }
    defaults.update(extra)
    return DocumentRecord(**defaults)


def test_resolve_returns_document_overlay_when_present(tmp_path: Path) -> None:
    doc_file = tmp_path / "doc-voice.md"
    doc_file.write_text("DOC LEVEL VOICE", encoding="utf-8")
    runtime_file = tmp_path / "runtime-voice.md"
    runtime_file.write_text("RUNTIME LEVEL VOICE", encoding="utf-8")

    document = _make_document(overlay_path=str(doc_file))
    config = AppConfig(runtime=RuntimeConfig(style_overlay_path=str(runtime_file)))
    assert resolve_style_overlay(document, config) == "DOC LEVEL VOICE"


def test_resolve_falls_back_to_runtime_when_document_overlay_none(
    tmp_path: Path,
) -> None:
    runtime_file = tmp_path / "runtime-voice.md"
    runtime_file.write_text("RUNTIME LEVEL VOICE", encoding="utf-8")
    document = _make_document(overlay_path=None)
    config = AppConfig(runtime=RuntimeConfig(style_overlay_path=str(runtime_file)))
    assert resolve_style_overlay(document, config) == "RUNTIME LEVEL VOICE"


def test_resolve_returns_none_when_neither_configured() -> None:
    document = _make_document(overlay_path=None)
    config = AppConfig()
    assert resolve_style_overlay(document, config) is None


def test_resolve_falls_back_when_document_path_missing(tmp_path: Path) -> None:
    """If the document's overlay file no longer exists on disk, fall back
    to the runtime overlay rather than silently dropping voice guidance."""
    runtime_file = tmp_path / "runtime-voice.md"
    runtime_file.write_text("RUNTIME FALLBACK", encoding="utf-8")
    document = _make_document(overlay_path=str(tmp_path / "missing.md"))
    config = AppConfig(runtime=RuntimeConfig(style_overlay_path=str(runtime_file)))
    assert resolve_style_overlay(document, config) == "RUNTIME FALLBACK"


def test_resolve_accepts_none_document() -> None:
    config = AppConfig()
    assert resolve_style_overlay(None, config) is None


def test_resolve_returns_runtime_when_no_document() -> None:
    runtime_file = Path("/tmp/r.md")
    runtime_file.write_text("RUNTIME ONLY", encoding="utf-8")
    config = AppConfig(runtime=RuntimeConfig(style_overlay_path=str(runtime_file)))
    try:
        assert resolve_style_overlay(None, config) == "RUNTIME ONLY"
    finally:
        runtime_file.unlink(missing_ok=True)
