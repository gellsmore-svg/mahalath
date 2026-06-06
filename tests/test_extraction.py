"""Tests for candidate term extraction."""

from __future__ import annotations

import pytest

from mahalath.adapters import MockAdapter
from mahalath.extraction import (
    CandidateTerm,
    ExtractionError,
    build_extraction_prompt,
    extract_candidate_terms,
    parse_candidates,
)


def test_build_prompt_truncates_long_documents() -> None:
    text = "x" * 60_000
    prompt = build_extraction_prompt(text, char_budget=50_000)
    assert "document truncated" in prompt
    assert prompt.count("x") <= 50_010  # 50k chars plus a few in the marker


def test_build_prompt_does_not_truncate_short_documents() -> None:
    text = "An agonist activates the receptor."
    prompt = build_extraction_prompt(text)
    assert text in prompt
    assert "truncated" not in prompt


def test_parse_candidates_happy_path() -> None:
    raw = (
        '{"candidates": ['
        ' {"term": "agonist", "context": "An agonist activates a receptor."},'
        ' {"term": "partial agonist", "context": "Submaximal response."}'
        ']}'
    )
    result = parse_candidates(raw)
    assert result == [
        CandidateTerm(term="agonist", context="An agonist activates a receptor."),
        CandidateTerm(term="partial agonist", context="Submaximal response."),
    ]


def test_parse_candidates_tolerates_preamble() -> None:
    raw = (
        "Here is the JSON you asked for:\n"
        '{"candidates": [{"term": "agonist", "context": "..."}]}'
        "\nLet me know if you need more!"
    )
    result = parse_candidates(raw)
    assert len(result) == 1
    assert result[0].term == "agonist"


def test_parse_candidates_dedupes_case_insensitively() -> None:
    raw = (
        '{"candidates": ['
        ' {"term": "Agonist", "context": "first"},'
        ' {"term": "AGONIST", "context": "second"},'
        ' {"term": "agonist", "context": "third"}'
        ']}'
    )
    result = parse_candidates(raw)
    assert len(result) == 1
    assert result[0].term == "Agonist"  # first occurrence wins


def test_parse_candidates_skips_empty_terms() -> None:
    raw = (
        '{"candidates": ['
        ' {"term": "", "context": "blank"},'
        ' {"term": "   ", "context": "whitespace"},'
        ' {"term": "agonist", "context": "valid"}'
        ']}'
    )
    result = parse_candidates(raw)
    assert [c.term for c in result] == ["agonist"]


def test_parse_candidates_respects_max_terms() -> None:
    items = [{"term": f"term-{i}", "context": "x"} for i in range(50)]
    raw = '{"candidates": ' + str(items).replace("'", '"') + "}"
    result = parse_candidates(raw, max_terms=5)
    assert len(result) == 5


def test_parse_candidates_raises_on_no_json() -> None:
    with pytest.raises(ExtractionError):
        parse_candidates("just prose, no JSON object here.")


def test_parse_candidates_raises_on_missing_list() -> None:
    with pytest.raises(ExtractionError):
        parse_candidates('{"other_key": "value"}')


def test_extract_candidate_terms_uses_adapter() -> None:
    adapter = MockAdapter(
        responses={
            "domain-glossary extractor": (
                '{"candidates": [{"term": "agonist", "context": "drug"}]}'
            ),
        }
    )
    result = extract_candidate_terms("An agonist activates a receptor.", adapter)
    assert result == [CandidateTerm(term="agonist", context="drug")]
    assert len(adapter.calls) == 1
    assert adapter.calls[0]["want_json"] is True


def test_extract_candidate_terms_filters_prompt_leaks() -> None:
    """Defensive filter: terms that don't appear in the document are dropped.

    This catches the gemma3:1b failure mode of returning the prompt's
    own meta-vocabulary as candidates.
    """
    adapter = MockAdapter(
        responses={
            "domain-glossary extractor": (
                '{"candidates": ['
                ' {"term": "agonist", "context": "real"},'
                ' {"term": "multi-word phrase", "context": "leaked"},'
                ' {"term": "technical noun", "context": "leaked"}'
                ']}'
            ),
        }
    )
    result = extract_candidate_terms(
        "An agonist activates a receptor.", adapter
    )
    assert [c.term for c in result] == ["agonist"]


def test_extract_candidate_terms_filter_can_be_disabled() -> None:
    adapter = MockAdapter(
        responses={
            "domain-glossary extractor": (
                '{"candidates": [{"term": "synonymous-paraphrase", "context": "x"}]}'
            ),
        }
    )
    result = extract_candidate_terms(
        "Some document text.",
        adapter,
        require_term_in_document=False,
    )
    assert [c.term for c in result] == ["synonymous-paraphrase"]
