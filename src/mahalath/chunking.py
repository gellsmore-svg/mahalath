"""Heading-aware Markdown chunking for the extraction pipeline.

The single-shot extraction prompt cannot exceed ~30 K characters on
gemma4:e2b before the model stops following instructions and starts
summarising the document instead. Real books are much larger:

  rs-cbo-beginner-v1.md       46 KB
  rs-cbo-gcse-v2.md          100 KB
  rs-cbo-alevel-v2.md        137 KB
  rs-master-book-v1.md       197 KB
  rs-technical-v1.md         218 KB
  rs-cbo-bachelors-v2.md     270 KB

This module turns one document into a list of self-contained chunks
that the extractor can process individually. Strategy:

  1. Primary split at H2 boundaries (`## ...` lines) so each chunk
     is one or more complete top-level sections.
  2. If a single H2 section is bigger than the chunk budget, fall back
     to splitting it on blank-line paragraph boundaries.
  3. Greedily pack consecutive small sections into the same chunk up
     to the budget.

The extractor's per-chunk prompts stay under the budget; the
aggregation step dedupes candidate terms across chunks, filtering
against the FULL document so a candidate proposed in chunk K because
of context anchored in chunk K-1 is still accepted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mahalath.adapters.base import Adapter
from mahalath.extraction import (
    CandidateTerm,
    DEFAULT_MAX_TERMS,
    ExtractionError,
    build_extraction_prompt,
    parse_candidates,
)

DEFAULT_CHUNK_CHARS = 30_000
DEFAULT_MAX_TOTAL_TERMS = 100

_H2_SPLIT = re.compile(r"\n(?=## )")
_PARAGRAPH_SPLIT = re.compile(r"\n{2,}")


@dataclass(frozen=True)
class Chunk:
    text: str
    index: int
    char_start: int
    char_end: int

    @property
    def size(self) -> int:
        return len(self.text)


def chunk_markdown(text: str, *, max_chunk_chars: int = DEFAULT_CHUNK_CHARS) -> list[Chunk]:
    """Split `text` into chunks under `max_chunk_chars`, biased to heading boundaries."""
    if not text:
        return []
    if max_chunk_chars <= 0:
        raise ValueError("max_chunk_chars must be > 0")

    raw_sections = _split_at_h2(text)

    pieces: list[str] = []
    for section in raw_sections:
        if len(section) <= max_chunk_chars:
            pieces.append(section)
            continue
        # Section alone exceeds budget — fall back to paragraph split.
        pieces.extend(_split_oversized(section, max_chunk_chars))

    # Greedily pack consecutive pieces.
    chunks: list[Chunk] = []
    current: list[str] = []
    current_size = 0
    char_cursor = 0
    chunk_start = 0

    for piece in pieces:
        piece_len = len(piece)
        # +1 accounts for the rejoining newline.
        joined_len = piece_len + (1 if current else 0)
        if current and current_size + joined_len > max_chunk_chars:
            chunk_text = "\n".join(current)
            chunks.append(Chunk(
                text=chunk_text,
                index=len(chunks),
                char_start=chunk_start,
                char_end=chunk_start + len(chunk_text),
            ))
            char_cursor = chunk_start + len(chunk_text) + 1
            current = [piece]
            current_size = piece_len
            chunk_start = char_cursor
        else:
            current.append(piece)
            current_size += joined_len if current else piece_len

    if current:
        chunk_text = "\n".join(current)
        chunks.append(Chunk(
            text=chunk_text,
            index=len(chunks),
            char_start=chunk_start,
            char_end=chunk_start + len(chunk_text),
        ))

    return chunks


def _split_at_h2(text: str) -> list[str]:
    """Split at lines starting with `## `. Preserves the heading on each part."""
    parts = _H2_SPLIT.split(text)
    return [p for p in parts if p.strip()]


def _split_oversized(section: str, max_chunk_chars: int) -> list[str]:
    """Split a single oversized section by blank-line paragraphs.

    If a single paragraph is itself larger than max_chunk_chars (rare
    but possible for code blocks etc.) it is emitted as-is — better to
    overshoot the budget than to break the paragraph mid-sentence.
    """
    paragraphs = [p for p in _PARAGRAPH_SPLIT.split(section) if p.strip()]
    result: list[str] = []
    current: list[str] = []
    current_size = 0
    for paragraph in paragraphs:
        para_len = len(paragraph)
        joined_len = para_len + (2 if current else 0)
        if current and current_size + joined_len > max_chunk_chars:
            result.append("\n\n".join(current))
            current = [paragraph]
            current_size = para_len
        else:
            current.append(paragraph)
            current_size += joined_len if current else para_len
    if current:
        result.append("\n\n".join(current))
    return result


def extract_candidates_chunked(
    text: str,
    adapter: Adapter,
    *,
    max_chunk_chars: int = DEFAULT_CHUNK_CHARS,
    max_terms_per_chunk: int = DEFAULT_MAX_TERMS,
    max_total_terms: int = DEFAULT_MAX_TOTAL_TERMS,
    model: str | None = None,
    require_term_in_document: bool = True,
    style_overlay: str | None = None,
) -> list[CandidateTerm]:
    """Chunk a document, extract per chunk, aggregate.

    Aggregation policy:
      - Dedupe by casefold term across chunks; keep the first occurrence.
      - When `require_term_in_document` is set, candidates are checked
        against the FULL document (not the chunk that produced them) so
        a term mentioned in chunk K-1 and proposed during chunk K still
        passes.
      - Hard cap at `max_total_terms` across all chunks.
    """
    chunks = chunk_markdown(text, max_chunk_chars=max_chunk_chars)
    if not chunks:
        return []

    full_doc_lower = text.casefold() if require_term_in_document else ""
    aggregated: list[CandidateTerm] = []
    seen: set[str] = set()

    for chunk in chunks:
        # build_extraction_prompt's own char_budget mustn't truncate the
        # chunk we just carefully sized to fit.
        prompt = build_extraction_prompt(
            chunk.text,
            max_terms=max_terms_per_chunk,
            char_budget=max(len(chunk.text), max_chunk_chars) + 1024,
            style_overlay=style_overlay,
        )
        response = adapter.generate(prompt, want_json=True, model=model)
        try:
            chunk_candidates = parse_candidates(
                response.text, max_terms=max_terms_per_chunk
            )
        except ExtractionError:
            # One bad chunk should not abort the whole pipeline; the
            # caller can read the activity log and re-process. The
            # alternative — raising — would lose any candidates already
            # gathered from earlier chunks.
            continue

        for candidate in chunk_candidates:
            if (
                require_term_in_document
                and candidate.term.casefold() not in full_doc_lower
            ):
                continue
            key = candidate.term.casefold()
            if key in seen:
                continue
            seen.add(key)
            aggregated.append(candidate)
            if len(aggregated) >= max_total_terms:
                return aggregated

    return aggregated
