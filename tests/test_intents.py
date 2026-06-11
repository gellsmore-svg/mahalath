"""Intent taxonomy (I-A): kind discriminator, intent fields, seeding,
and — critically — isolation of the frame paths from intent rows.

Runs against a live MongoDB test database (the `mongo_db` fixture).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mahalath.db.models import (
    DefinitionContext,
    DefinitionVersion,
    OntologyEntry,
)
from mahalath.db.repositories import (
    DefinitionContextRepository,
    OntologyEntryRepository,
)
from mahalath.intents import (
    STANDARD_INTENTS,
    resolve_intent_tag,
    seed_intents,
)


# --- Model layer ------------------------------------------------------------


def test_definition_context_kind_defaults_to_frame() -> None:
    ctx = DefinitionContext(name="structural", description="x")
    assert ctx.kind == "frame"


def test_legacy_context_doc_reads_as_frame(mongo_db) -> None:
    # Pre-I-A documents have no `kind` field at all.
    mongo_db.definition_contexts.insert_one({
        "context_id": "legacy-1", "name": "structural",
        "description": "x", "created_by": "operator",
        "schema_version": 1,
    })
    repo = DefinitionContextRepository(mongo_db)
    assert repo.get("legacy-1").kind == "frame"
    assert [c.context_id for c in repo.all(kind="frame")] == ["legacy-1"]
    assert repo.all(kind="intent") == []


def test_definition_version_intent_fields_default_empty() -> None:
    d = DefinitionVersion(text="x")
    assert d.intent_tags == []
    assert d.intentionality is None
    assert d.intent_confidence is None


def test_intentionality_is_ordinal_never_float() -> None:
    # ADR-025: low | medium | high only.
    DefinitionVersion(text="x", intentionality="high")
    with pytest.raises(ValidationError):
        DefinitionVersion(text="x", intentionality=0.83)
    with pytest.raises(ValidationError):
        DefinitionVersion(text="x", intentionality="very high")


# --- Repository kind filtering ----------------------------------------------


def test_repo_all_filters_by_kind(mongo_db) -> None:
    repo = DefinitionContextRepository(mongo_db)
    repo.insert(DefinitionContext(name="structural", description="x"))
    repo.insert(DefinitionContext(name="teach", description="y", kind="intent"))
    # Set comparisons: created_at sort ties at Mongo's ms precision, so
    # same-millisecond inserts have no stable relative order.
    assert {c.name for c in repo.all()} == {"structural", "teach"}
    assert [c.name for c in repo.all(kind="frame")] == ["structural"]
    assert [c.name for c in repo.all(kind="intent")] == ["teach"]


def test_repo_get_by_name_kind_filter(mongo_db) -> None:
    repo = DefinitionContextRepository(mongo_db)
    repo.insert(DefinitionContext(name="teach", description="y", kind="intent"))
    assert repo.get_by_name("teach").kind == "intent"
    assert repo.get_by_name("teach", kind="intent") is not None
    assert repo.get_by_name("teach", kind="frame") is None


# --- Seeding ------------------------------------------------------------------


def test_seed_intents_inserts_standard_set(mongo_db) -> None:
    result = seed_intents(mongo_db)
    assert len(result.inserted) == len(STANDARD_INTENTS)
    repo = DefinitionContextRepository(mongo_db)
    intents = repo.all(kind="intent")
    assert {c.name for c in intents} == {n for n, _ in STANDARD_INTENTS}
    assert all(c.created_by == "operator" for c in intents)


def test_seed_intents_idempotent(mongo_db) -> None:
    seed_intents(mongo_db)
    second = seed_intents(mongo_db)
    assert second.inserted == []
    assert len(second.skipped_existing) == len(STANDARD_INTENTS)
    assert mongo_db.definition_contexts.count_documents(
        {"kind": "intent"}
    ) == len(STANDARD_INTENTS)


def test_seed_intents_dry_run_writes_nothing(mongo_db) -> None:
    result = seed_intents(mongo_db, dry_run=True)
    assert len(result.inserted) == len(STANDARD_INTENTS)
    assert mongo_db.definition_contexts.count_documents({}) == 0


def test_seed_intents_reports_frame_name_conflict(mongo_db) -> None:
    # A FRAME already named "teach": one namespace, so the seeder must
    # flag it for the operator instead of silently skipping.
    DefinitionContextRepository(mongo_db).insert(
        DefinitionContext(name="teach", description="a frame, oddly")
    )
    result = seed_intents(mongo_db)
    assert "teach" in result.name_conflicts
    assert "teach" not in result.inserted
    # The frame is untouched.
    assert DefinitionContextRepository(mongo_db).get_by_name("teach").kind == "frame"


# --- resolve_intent_tag -------------------------------------------------------


def test_resolve_intent_tag_by_name_and_id(mongo_db) -> None:
    seed_intents(mongo_db)
    by_name = resolve_intent_tag(mongo_db, "teach")
    assert by_name is not None
    assert resolve_intent_tag(mongo_db, by_name) == by_name  # by id too


def test_resolve_intent_tag_rejects_frames_and_unknown(mongo_db) -> None:
    repo = DefinitionContextRepository(mongo_db)
    repo.insert(DefinitionContext(name="structural", description="x"))
    assert resolve_intent_tag(mongo_db, "structural") is None  # frame
    assert resolve_intent_tag(mongo_db, "nonexistent") is None


# --- Frame-path isolation (the load-bearing I-A property) --------------------


def test_debate_context_resolution_ignores_intents(mongo_db) -> None:
    # A debate returning "teach" as its frame must NOT tag the
    # definition with the intent row (ADR-024).
    from mahalath.ontology import _resolve_context_id
    seed_intents(mongo_db)
    assert _resolve_context_id(mongo_db, "teach") is None
    repo = DefinitionContextRepository(mongo_db)
    repo.insert(DefinitionContext(name="structural", description="x"))
    assert _resolve_context_id(mongo_db, "structural") is not None


def test_backfill_contexts_ignores_intent_rows(mongo_config, mongo_db) -> None:
    # With ONLY intents defined, the frame backfill sees no contexts and
    # must no-op (adapter never called).
    from mahalath.adapters import MockAdapter
    from mahalath.staleness import backfill_definition_contexts

    seed_intents(mongo_db)
    OntologyEntryRepository(mongo_db).insert(OntologyEntry(
        mpl_label="MPL-001", canonical_term="alpha", confidence=8.0,
        definitions=[DefinitionVersion(text="Untagged.")],
    ))

    class ExplodingAdapter(MockAdapter):
        def generate(self, *a, **k):  # pragma: no cover - must not run
            raise AssertionError("frame backfill consulted the adapter "
                                 "with only intent rows defined")

    result = backfill_definition_contexts(
        mongo_config, mongo_db, ExplodingAdapter(), apply=True,
    )
    assert result.applied == 0


def test_frame_scoped_ref_rejects_intent_name(mongo_db) -> None:
    # MPL-x#teach must not resolve — handles name meaning frames only.
    from mahalath.retrieval import get_codified
    seed_intents(mongo_db)
    repo = DefinitionContextRepository(mongo_db)
    frame = DefinitionContext(name="structural", description="x")
    repo.insert(frame)
    OntologyEntryRepository(mongo_db).insert(OntologyEntry(
        mpl_label="MPL-001", canonical_term="alpha", confidence=8.0,
        definitions=[DefinitionVersion(
            text="Alpha.", context_id=frame.context_id,
        )],
    ))
    assert get_codified(mongo_db, "MPL-001#structural") is not None
    assert get_codified(mongo_db, "MPL-001#teach") is None


def test_chat_context_map_excludes_intents(mongo_db) -> None:
    # The chat prompt's frame grouping must not pull intent rows in.
    import json as _json
    from mahalath.adapters import MockAdapter
    from mahalath.chat import answer_question

    seed_intents(mongo_db)
    repo = DefinitionContextRepository(mongo_db)
    frame = DefinitionContext(name="structural", description="Frame desc.")
    repo.insert(frame)
    OntologyEntryRepository(mongo_db).insert(OntologyEntry(
        mpl_label="MPL-001", canonical_term="alpha", confidence=8.0,
        definitions=[DefinitionVersion(
            text="Alpha def.", context_id=frame.context_id,
        )],
    ))

    captured: dict = {}

    class CapturingAdapter(MockAdapter):
        def generate(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return super().generate(prompt, **kwargs)

    adapter = CapturingAdapter(default_response=_json.dumps(
        {"answer": "ok", "suggested_actions": []}
    ))
    answer_question("what is alpha?", mongo_db, adapter)
    assert "Context [structural]" in captured["prompt"]
    # No intent tag leaks into the frame-grouped context block.
    assert "Context [teach]" not in captured["prompt"]
