"""Chunking and chunked-extraction tests."""

from __future__ import annotations

import pytest

from mahalath.adapters import MockAdapter
from mahalath.chunking import (
    Chunk,
    chunk_markdown,
    extract_candidates_chunked,
)


# --- chunk_markdown --------------------------------------------------------


def test_chunk_markdown_empty_input() -> None:
    assert chunk_markdown("") == []


def test_chunk_markdown_small_document_is_one_chunk() -> None:
    text = "# Title\n\n## Section A\n\nSome text.\n\n## Section B\n\nMore text.\n"
    chunks = chunk_markdown(text, max_chunk_chars=10_000)
    assert len(chunks) == 1
    assert chunks[0].index == 0
    assert chunks[0].text.strip().endswith("More text.")


def test_chunk_markdown_splits_at_h2_boundaries() -> None:
    big_section = "A " * 6000  # ~12 K chars
    text = (
        "# Doc\n\n"
        + f"## First section\n\n{big_section}\n\n"
        + f"## Second section\n\n{big_section}\n\n"
        + f"## Third section\n\n{big_section}\n\n"
    )
    chunks = chunk_markdown(text, max_chunk_chars=15_000)
    # Three ~12K sections, 15K budget — each section gets its own chunk.
    assert len(chunks) == 3
    # All chunks start at a heading boundary
    for chunk in chunks:
        assert "## " in chunk.text


def test_chunk_markdown_packs_small_sections_together() -> None:
    small = "## S{i}\n\n" + ("x " * 200)  # ~408 chars per section
    text = "\n".join(small.format(i=i) for i in range(20))
    chunks = chunk_markdown(text, max_chunk_chars=5_000)
    # 20 small sections of ~400 each: should pack into a handful of chunks.
    assert 1 < len(chunks) < 20


def test_chunk_markdown_falls_back_to_paragraph_split_on_oversize_section() -> None:
    big_paragraph_section = "## Huge\n\n" + "\n\n".join(
        "P " * 1000 for _ in range(5)
    )
    chunks = chunk_markdown(big_paragraph_section, max_chunk_chars=5_000)
    # Single H2 section but the paragraph splitter divides it.
    assert len(chunks) >= 2


def test_chunk_markdown_assigns_sequential_indices() -> None:
    text = "## A\n\n" + ("x " * 3000) + "\n\n## B\n\n" + ("y " * 3000)
    chunks = chunk_markdown(text, max_chunk_chars=4_000)
    indices = [c.index for c in chunks]
    assert indices == list(range(len(chunks)))


def test_chunk_markdown_rejects_zero_budget() -> None:
    with pytest.raises(ValueError):
        chunk_markdown("text", max_chunk_chars=0)


# --- extract_candidates_chunked --------------------------------------------


def _json(candidates: list[dict]) -> str:
    import json
    return json.dumps({"candidates": candidates})


def test_extract_candidates_chunked_aggregates_across_chunks() -> None:
    text = (
        "## Section A\n\nThe alpha concept is fundamental.\n\n" * 1500
        + "## Section B\n\nThe beta variant differs.\n\n" * 1500
    )
    # Chunks at 20 K → likely 2 chunks.
    adapter = MockAdapter(
        responses={
            "Section A": _json([{"term": "alpha concept", "context": "fundamental"}]),
            "Section B": _json([{"term": "beta variant", "context": "differs"}]),
        }
    )
    result = extract_candidates_chunked(text, adapter, max_chunk_chars=20_000)
    terms = sorted(c.term for c in result)
    assert "alpha concept" in terms
    assert "beta variant" in terms


def test_extract_candidates_chunked_dedupes_terms_seen_in_multiple_chunks() -> None:
    text = (
        "## A\n\nThe ubiquitous term is everywhere.\n\n" * 1500
        + "## B\n\nThe ubiquitous term again.\n\n" * 1500
    )
    adapter = MockAdapter(
        responses={
            "domain-glossary": _json([
                {"term": "ubiquitous term", "context": "everywhere"},
            ]),
        }
    )
    result = extract_candidates_chunked(text, adapter, max_chunk_chars=20_000)
    # Only one entry even though both chunks proposed it.
    assert len([c for c in result if c.term == "ubiquitous term"]) == 1


def test_extract_candidates_chunked_filter_uses_full_document() -> None:
    """A candidate mentioned only in chunk 1 but proposed during chunk 2
    should still pass the require_term_in_document filter."""
    text = (
        "## A\n\nUnique anchor word here.\n\n" * 1500
        + "## B\n\nNo anchor here.\n\n" * 1500
    )
    adapter = MockAdapter(
        responses={
            "domain-glossary": _json([
                {"term": "anchor word", "context": "in chunk A"},
            ]),
        }
    )
    result = extract_candidates_chunked(text, adapter, max_chunk_chars=20_000)
    # "anchor word" appears in the document; should survive.
    assert any(c.term == "anchor word" for c in result)


def test_extract_candidates_chunked_filter_drops_words_absent_from_document() -> None:
    text = (
        "## A\n\nReal content.\n\n" * 1500
        + "## B\n\nMore real content.\n\n" * 1500
    )
    adapter = MockAdapter(
        responses={
            "domain-glossary": _json([
                {"term": "imaginary phrase", "context": "fabricated"},
                {"term": "real", "context": "real"},
            ]),
        }
    )
    result = extract_candidates_chunked(text, adapter, max_chunk_chars=20_000)
    terms = [c.term for c in result]
    assert "real" in terms
    assert "imaginary phrase" not in terms


def test_extract_candidates_chunked_respects_max_total() -> None:
    text = "## A\n\nWord here.\n\n" * 2000
    adapter = MockAdapter(
        responses={
            "domain-glossary": _json(
                [{"term": f"term-{i}", "context": "x"} for i in range(50)]
            ),
        }
    )
    # max_total_terms = 5 caps the aggregated list.
    result = extract_candidates_chunked(
        text, adapter, max_chunk_chars=20_000,
        max_total_terms=5, require_term_in_document=False,
    )
    assert len(result) == 5


def test_extract_candidates_chunked_skips_failed_chunks() -> None:
    """If one chunk's extraction returns garbage, surviving chunks still aggregate."""
    text = (
        "## Section A\n\n"
        + ("alpha content here. " * 800)
        + "\n\n## DIFFERENT B\n\n"
        + ("beta content here. " * 800)
    )
    # Two ~16K sections; at 18K budget each lands in its own chunk.
    adapter = MockAdapter(
        default_response="not valid json at all",
        responses={
            "Section A": _json([{"term": "alpha", "context": "good"}]),
        }
    )
    result = extract_candidates_chunked(text, adapter, max_chunk_chars=18_000)
    # Chunk A matched the "Section A" needle and returned good JSON.
    # Chunk B fell through to default_response (bad JSON) — should not crash.
    assert any(c.term == "alpha" for c in result)
