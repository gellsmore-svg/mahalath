"""Style overlay loader + injection tests."""

from __future__ import annotations

from pathlib import Path

from mahalath.config import AppConfig, RuntimeConfig
from mahalath.debate import _build_critic_prompt, _build_explorer_prompt
from mahalath.db.models import DebateMessage, DefinitionVersion, OntologyEntry
from mahalath.extraction import build_extraction_prompt
from mahalath.hierarchy import build_review_prompt
from mahalath.style import (
    STYLE_OVERLAY_FOOTER,
    STYLE_OVERLAY_HEADER,
    load_style_overlay,
    render_style_block,
)


# --- Loader -----------------------------------------------------------------


def test_load_style_overlay_returns_none_when_not_configured(
    tmp_path: Path,
) -> None:
    config = AppConfig()
    assert load_style_overlay(config, project_root=tmp_path) is None


def test_load_style_overlay_returns_none_when_file_missing(
    tmp_path: Path,
) -> None:
    config = AppConfig(
        runtime=RuntimeConfig(style_overlay_path="nonexistent.md"),
    )
    assert load_style_overlay(config, project_root=tmp_path) is None


def test_load_style_overlay_reads_relative_path(tmp_path: Path) -> None:
    overlay_file = tmp_path / "docs" / "style.md"
    overlay_file.parent.mkdir()
    overlay_file.write_text("# Style\n\nVoice notes here.\n", encoding="utf-8")
    config = AppConfig(
        runtime=RuntimeConfig(style_overlay_path="docs/style.md"),
    )
    loaded = load_style_overlay(config, project_root=tmp_path)
    assert loaded is not None
    assert "Voice notes here." in loaded


def test_load_style_overlay_reads_absolute_path(tmp_path: Path) -> None:
    overlay_file = tmp_path / "style.md"
    overlay_file.write_text("Absolute path content.", encoding="utf-8")
    config = AppConfig(
        runtime=RuntimeConfig(style_overlay_path=str(overlay_file)),
    )
    assert "Absolute path content." in (load_style_overlay(config) or "")


# --- render_style_block ---------------------------------------------------


def test_render_style_block_returns_empty_for_none() -> None:
    assert render_style_block(None) == ""


def test_render_style_block_returns_empty_for_whitespace() -> None:
    assert render_style_block("   \n  \n") == ""


def test_render_style_block_wraps_with_header_and_footer() -> None:
    block = render_style_block("This is the overlay.")
    assert STYLE_OVERLAY_HEADER in block
    assert STYLE_OVERLAY_FOOTER in block
    assert "This is the overlay." in block


# --- Injection sites ------------------------------------------------------


def test_extraction_prompt_includes_overlay_when_provided() -> None:
    prompt = build_extraction_prompt(
        "Document text here.",
        style_overlay="Definitions should be declarative.",
    )
    assert STYLE_OVERLAY_HEADER in prompt
    assert "declarative" in prompt
    # Original document body still present.
    assert "Document text here." in prompt


def test_extraction_prompt_unchanged_when_no_overlay() -> None:
    prompt_a = build_extraction_prompt("Doc text.")
    prompt_b = build_extraction_prompt("Doc text.", style_overlay=None)
    prompt_c = build_extraction_prompt("Doc text.", style_overlay="")
    assert prompt_a == prompt_b == prompt_c
    assert STYLE_OVERLAY_HEADER not in prompt_a


def test_critic_prompt_includes_overlay() -> None:
    prompt = _build_critic_prompt(
        "agonist", "context", history=[],
        style_overlay="Be conservative with metaphor.",
    )
    assert STYLE_OVERLAY_HEADER in prompt
    assert "conservative with metaphor" in prompt


def test_critic_prompt_unchanged_without_overlay() -> None:
    a = _build_critic_prompt("agonist", "context", history=[])
    b = _build_critic_prompt(
        "agonist", "context", history=[], style_overlay=None
    )
    assert a == b


def test_explorer_prompt_includes_overlay() -> None:
    prompt = _build_explorer_prompt(
        "agonist", "context",
        history=[DebateMessage(iteration=1, role="precision_critic", content="...")],
        style_overlay="Integrate critic's concerns.",
    )
    assert STYLE_OVERLAY_HEADER in prompt
    assert "Integrate critic" in prompt


def test_review_prompt_includes_overlay() -> None:
    focus = OntologyEntry(
        mpl_label="MPL-001", canonical_term="Substrate",
        confidence=8.0,
        definitions=[DefinitionVersion(text="Underlying medium.")],
    )
    prompt = build_review_prompt(
        [], focus, style_overlay="Hierarchy: more-specific are children.",
    )
    assert STYLE_OVERLAY_HEADER in prompt
    assert "more-specific are children" in prompt
