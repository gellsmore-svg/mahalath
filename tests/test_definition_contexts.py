"""Definition context schema + chat grouping tests."""

from __future__ import annotations

import json as _json


from mahalath.adapters import MockAdapter
from mahalath.chat import answer_question, build_chat_prompt
from mahalath.db.models import (
    DefinitionContext,
    DefinitionVersion,
    OntologyEntry,
)
from mahalath.db.repositories import (
    DefinitionContextRepository,
    OntologyEntryRepository,
)


def test_definition_context_repo_round_trip(mongo_db) -> None:
    ctx = DefinitionContext(
        name="theological",
        description="Definitions framed within the corpus's biblical / scriptural framing.",
        created_by="operator",
    )
    DefinitionContextRepository(mongo_db).insert(ctx)
    fetched = DefinitionContextRepository(mongo_db).get(ctx.context_id)
    assert fetched is not None
    assert fetched.name == "theological"
    assert fetched.description.startswith("Definitions framed")


def test_definition_context_lookup_by_name(mongo_db) -> None:
    ctx = DefinitionContext(name="structural", description="Generic structural frame.")
    DefinitionContextRepository(mongo_db).insert(ctx)
    fetched = DefinitionContextRepository(mongo_db).get_by_name("structural")
    assert fetched is not None
    assert fetched.context_id == ctx.context_id


def test_definition_version_carries_context_id(mongo_db) -> None:
    ctx = DefinitionContext(name="physical", description="Physical frame.")
    DefinitionContextRepository(mongo_db).insert(ctx)
    entry = OntologyEntry(
        mpl_label="MPL-001",
        canonical_term="substrate",
        confidence=8.0,
        definitions=[
            DefinitionVersion(
                text="The configurations that allow vortons.",
                model_used="operator",
                context_id=ctx.context_id,
            ),
        ],
    )
    OntologyEntryRepository(mongo_db).insert(entry)
    stored = OntologyEntryRepository(mongo_db).get("MPL-001")
    assert stored.definitions[0].context_id == ctx.context_id


def test_chat_prompt_groups_definitions_by_context(mongo_db) -> None:
    ctx_th = DefinitionContext(
        name="theological",
        description="Definitions in the corpus's biblical framing.",
    )
    ctx_st = DefinitionContext(
        name="structural",
        description="Definitions in the generic structural framing.",
    )
    repo = DefinitionContextRepository(mongo_db)
    repo.insert(ctx_th)
    repo.insert(ctx_st)
    entries_repo = OntologyEntryRepository(mongo_db)
    entries_repo.insert(OntologyEntry(
        mpl_label="MPL-004", canonical_term="substrate", confidence=8.0,
        definitions=[
            DefinitionVersion(
                text="The generic underlying medium.",
                model_used="gemma4:e2b",
                context_id=ctx_st.context_id,
            ),
            DefinitionVersion(
                text="The grammatical mechanism of creaturely existence.",
                model_used="operator",
                context_id=ctx_th.context_id,
            ),
        ],
    ))

    entries = [entries_repo.get("MPL-004")]
    contexts_map = {c.context_id: c for c in repo.all()}
    prompt = build_chat_prompt(
        "What is substrate?", entries, definition_contexts=contexts_map,
    )
    assert "Context [theological]" in prompt
    assert "Context [structural]" in prompt
    assert "biblical framing" in prompt
    assert "co-equal" in prompt
    # Both definitions present
    assert "generic underlying medium" in prompt
    assert "grammatical mechanism of creaturely existence" in prompt


def test_chat_prompt_handles_mix_of_contexted_and_uncontexted(mongo_db) -> None:
    ctx = DefinitionContext(name="general", description="Default frame.")
    repo = DefinitionContextRepository(mongo_db)
    repo.insert(ctx)
    entries_repo = OntologyEntryRepository(mongo_db)
    entries_repo.insert(OntologyEntry(
        mpl_label="MPL-001", canonical_term="alpha", confidence=8.0,
        definitions=[
            DefinitionVersion(text="With context", model_used="gemma4:e2b",
                              context_id=ctx.context_id),
            DefinitionVersion(text="Without context", model_used="operator"),
        ],
    ))
    contexts_map = {c.context_id: c for c in repo.all()}
    prompt = build_chat_prompt(
        "?", [entries_repo.get("MPL-001")], definition_contexts=contexts_map,
    )
    assert "Context [general]" in prompt
    assert "Context [unspecified]" in prompt


def test_chat_prompt_polysemy_note_present_when_contexts_defined() -> None:
    """Even with empty context list the polysemy note appears (it's general guidance)."""
    prompt = build_chat_prompt("test", [])
    assert "POLYSEMY NOTE" in prompt
    assert "CO-EQUAL" in prompt


def test_answer_question_pulls_contexts_from_db(mongo_db) -> None:
    """End-to-end: answer_question loads contexts and passes to the prompt builder."""
    ctx = DefinitionContext(name="theo", description="Theological frame.")
    DefinitionContextRepository(mongo_db).insert(ctx)
    OntologyEntryRepository(mongo_db).insert(OntologyEntry(
        mpl_label="MPL-001", canonical_term="Substrate", confidence=8.0,
        definitions=[DefinitionVersion(
            text="The grammatical mechanism of creaturely existence.",
            model_used="operator",
            context_id=ctx.context_id,
        )],
    ))

    envelope = _json.dumps({"answer": "From the theological frame... (MPL-001).",
                            "suggested_actions": []})
    adapter = MockAdapter(default_response=envelope)
    response = answer_question(
        "What is the substrate theologically?", mongo_db, adapter,
    )
    assert "MPL-001" in response.cited_labels
    # The prompt the adapter received should have the context info
    assert "theo" in adapter.calls[0]["prompt"]
    assert "Theological frame" in adapter.calls[0]["prompt"]


def test_append_operator_definition_with_context(mongo_db) -> None:
    from mahalath.staleness import append_operator_definition

    ctx = DefinitionContext(name="theological", description="...")
    DefinitionContextRepository(mongo_db).insert(ctx)
    OntologyEntryRepository(mongo_db).insert(OntologyEntry(
        mpl_label="MPL-001", canonical_term="alpha", confidence=8.0,
    ))
    append_operator_definition(
        mongo_db, "MPL-001",
        "theology says ...",
        context_id=ctx.context_id,
    )
    stored = OntologyEntryRepository(mongo_db).get("MPL-001")
    assert stored.definitions[-1].context_id == ctx.context_id
    assert stored.definitions[-1].model_used == "operator"
