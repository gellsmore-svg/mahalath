"""LLM-driven candidate term extraction.

Per DQ-004 the initial design uses the model itself to pick which terms
warrant a glossary entry, rather than a fixed regex / NER pipeline. The
model sees the whole document and returns a JSON list of candidates,
each carrying the term and the surrounding context snippet that
triggered the suggestion.

This module is deliberately a thin shim over the adapter: prompt
construction, response parsing, and a small amount of defensive
filtering (drop empty strings, trim whitespace, dedupe by case-folded
term). The debate loop in Stage 1.5 owns judgement; this module just
nominates.

For very long documents Stage 1 truncates to a generous character budget
(50,000 chars by default) rather than chunking. Chunking is deferred
until a real corpus actually exceeds the budget.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from mahalath.adapters.base import Adapter, AdapterError

DEFAULT_MAX_TERMS = 20
DEFAULT_CHAR_BUDGET = 50_000


@dataclass(frozen=True)
class CandidateTerm:
    term: str
    context: str

    def normalized(self) -> str:
        return self.term.strip().casefold()


class ExtractionError(Exception):
    """Raised when the adapter response cannot be parsed into candidates."""


def extract_candidate_terms(
    document_text: str,
    adapter: Adapter,
    *,
    max_terms: int = DEFAULT_MAX_TERMS,
    char_budget: int = DEFAULT_CHAR_BUDGET,
    model: str | None = None,
    require_term_in_document: bool = True,
) -> list[CandidateTerm]:
    """Return a deduped list of candidate terms suggested by the model.

    `require_term_in_document` filters out terms whose surface form does
    not appear (case-insensitively) in the source. This catches the
    common small-model failure where the prompt's meta-vocabulary
    ("multi-word phrase", "technical noun") leaks into the candidate
    list. Set False if a paraphrasing model is being used.
    """
    prompt = build_extraction_prompt(
        document_text, max_terms=max_terms, char_budget=char_budget
    )
    try:
        response = adapter.generate(prompt, want_json=True, model=model)
    except AdapterError as exc:
        raise ExtractionError(f"Adapter failed during extraction: {exc}") from exc
    candidates = parse_candidates(response.text, max_terms=max_terms)
    if require_term_in_document:
        lowered = document_text.casefold()
        candidates = [c for c in candidates if c.term.casefold() in lowered]
    return candidates


def build_extraction_prompt(
    document_text: str,
    *,
    max_terms: int = DEFAULT_MAX_TERMS,
    char_budget: int = DEFAULT_CHAR_BUDGET,
) -> str:
    """Construct the extraction prompt.

    Exposed so tests can assert prompt content and so the debate loop
    later can reuse the same builder for reflection passes.
    """
    truncated = document_text[:char_budget]
    if len(document_text) > char_budget:
        truncated += "\n[...document truncated...]\n"

    return (
        "You are a domain-glossary extractor. Read the document below and "
        "list the candidate terms that warrant their own glossary entry: "
        "technical nouns, named concepts, multi-word phrases of art, "
        "domain-specific jargon.\n"
        "\n"
        "Skip common English words, dates, numbers, generic verbs, and "
        "the author's prose voice. Prefer noun phrases as written in the "
        "source. Do not invent new terms.\n"
        "\n"
        f"Return AT MOST {max_terms} candidates. Output ONLY a JSON "
        "object of the form:\n"
        '  {"candidates": [ {"term": "<noun phrase>", "context": '
        '"<one-sentence snippet from the document>"}, ... ]}\n'
        "No preamble, no Markdown, no commentary outside the JSON.\n"
        "\n"
        "Document:\n"
        "<<<\n"
        f"{truncated}\n"
        ">>>\n"
    )


def parse_candidates(
    response_text: str,
    *,
    max_terms: int = DEFAULT_MAX_TERMS,
) -> list[CandidateTerm]:
    """Parse the adapter's JSON response into CandidateTerm objects.

    Defensive against:
      - leading/trailing prose ("Here is the JSON: ...")
      - extra whitespace around the JSON object
      - missing context field
      - duplicate terms (case-folded)
      - empty strings
    """
    payload = _extract_json_object(response_text)
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list):
        raise ExtractionError(
            "Adapter JSON did not contain a 'candidates' list; got "
            f"{type(raw_candidates).__name__}"
        )

    seen: set[str] = set()
    result: list[CandidateTerm] = []
    for entry in raw_candidates:
        if not isinstance(entry, dict):
            continue
        term = str(entry.get("term", "")).strip()
        context = str(entry.get("context", "")).strip()
        if not term:
            continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(CandidateTerm(term=term, context=context))
        if len(result) >= max_terms:
            break
    return result


def _extract_json_object(text: str) -> dict:
    """Locate the first balanced JSON object in text and parse it.

    Tolerates the common gemma3 failure mode of emitting a small
    prose preamble before the JSON despite the "JSON only" instruction.
    Raises ExtractionError on unrecoverable parse failures.
    """
    text = text.strip()
    if not text:
        raise ExtractionError("Adapter returned empty response.")

    # Fast path: pure JSON.
    try:
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            return loaded
    except json.JSONDecodeError:
        pass

    # Slow path: find the first '{' and scan to the matching '}'.
    start = text.find("{")
    if start == -1:
        raise ExtractionError(f"No JSON object found in response: {text[:200]!r}")

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError as exc:
                    raise ExtractionError(
                        f"Found JSON-like object but parse failed: {exc}"
                    ) from exc
    raise ExtractionError(
        f"Unbalanced braces while scanning response: {text[:200]!r}"
    )
