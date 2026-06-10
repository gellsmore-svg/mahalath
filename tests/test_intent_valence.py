"""Intent + valence guidance is present in debate and redefine prompts."""

from __future__ import annotations

from mahalath.db.models import DefinitionVersion, OntologyEntry
from mahalath.db.repositories import OntologyEntryRepository
from mahalath.debate import _build_critic_prompt, _build_explorer_prompt
from mahalath.staleness import build_redefine_prompt


def test_critic_prompt_carries_intent_and_valence_guidance() -> None:
    prompt = _build_critic_prompt("agonist", "context", history=[])
    assert "Semantic intent" in prompt
    assert "Semantic valence" in prompt
    assert "do NOT name them as fields" in prompt


def test_explorer_prompt_carries_intent_and_valence_guidance() -> None:
    prompt = _build_explorer_prompt("agonist", "context", history=[])
    assert "Semantic intent" in prompt
    assert "Semantic valence" in prompt


def test_redefine_prompt_carries_intent_and_valence_guidance(mongo_db) -> None:
    OntologyEntryRepository(mongo_db).insert(OntologyEntry(
        mpl_label="MPL-001", canonical_term="alpha", confidence=8.0,
        definitions=[DefinitionVersion(text="A foundational concept.")],
    ))
    entry = OntologyEntryRepository(mongo_db).get("MPL-001")
    prompt = build_redefine_prompt(entry, mongo_db, style_overlay=None)
    assert "Semantic intent" in prompt
    assert "Semantic valence" in prompt
