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


# --- I-B: N-pass attribution + backfill ---------------------------------------


import json as _json
from dataclasses import dataclass as _dataclass, field as _field

from mahalath.adapters.base import AdapterResponse
from mahalath.intents import (
    INTENT_ATTRIBUTION_TAG,
    attribute_intent,
    backfill_intents,
    build_intent_prompt,
    parse_intent_verdict,
)


@_dataclass
class _SeqAdapter:
    """Returns a different canned response per call (cycles)."""

    responses: list[str]
    name: str = "sequential_mock"
    default_model: str = "mock-model"
    calls: list = _field(default_factory=list)

    def __post_init__(self) -> None:
        self._index = 0

    def generate(self, prompt, *, model=None, timeout_seconds=None,
                 want_json=False):
        self.calls.append(prompt)
        text = self.responses[self._index % len(self.responses)]
        self._index += 1
        return AdapterResponse(
            text=text, model=model or self.default_model, duration_ms=0,
        )


def _verdict(tags, intentionality="high", confidence=9.0) -> str:
    return _json.dumps({
        "intent_tags": tags,
        "intentionality": intentionality,
        "confidence": confidence,
    })


def _seed_entry(mongo_db, label="MPL-001", term="alpha") -> None:
    OntologyEntryRepository(mongo_db).insert(OntologyEntry(
        mpl_label=label, canonical_term=term, confidence=8.0,
        definitions=[DefinitionVersion(text=f"{term} definition.")],
    ))


def test_build_intent_prompt_carries_taxonomy_and_marker(mongo_db) -> None:
    seed_intents(mongo_db)
    intents = DefinitionContextRepository(mongo_db).all(kind="intent")
    prompt = build_intent_prompt("alpha", "Alpha def.", intents)
    assert prompt.startswith(INTENT_ATTRIBUTION_TAG)
    assert "- teach:" in prompt
    assert "Alpha def." in prompt
    assert "intentionality" in prompt.lower()


def test_parse_intent_verdict_tolerant() -> None:
    assert parse_intent_verdict(_verdict(["teach"])).intent_tags == ["teach"]
    fenced = "```json\n" + _verdict(["warn"], "low", 7.5) + "\n```"
    v = parse_intent_verdict(fenced)
    assert v.intent_tags == ["warn"] and v.intentionality == "low"
    prosey = "Here you go: " + _verdict([], "medium", 6.0) + " hope that helps"
    assert parse_intent_verdict(prosey).intentionality == "medium"
    assert parse_intent_verdict("no json at all") is None
    # Out-of-vocabulary intentionality degrades to None, not an error.
    bad = _json.dumps({"intent_tags": [], "intentionality": "extreme",
                       "confidence": 9})
    assert parse_intent_verdict(bad).intentionality is None


def test_attribute_unanimous_stores(mongo_db) -> None:
    seed_intents(mongo_db)
    _seed_entry(mongo_db)
    adapter = _SeqAdapter(responses=[
        _verdict(["teach", "persuade"], "high", 9.0),
        _verdict(["teach"], "high", 8.5),
        _verdict(["teach", "warn"], "high", 9.5),
    ])
    a = attribute_intent(mongo_db, "MPL-001", 0, adapter, passes=3, apply=True)
    # Only the tag EVERY pass proposed survives.
    assert a.unanimous_tags == ["teach"]
    assert a.outcome == "stored" and a.stored is True
    assert a.intent_confidence == 8.5  # min across passes
    d = OntologyEntryRepository(mongo_db).get("MPL-001").definitions[0]
    assert d.intent_tags == a.unanimous_tag_ids
    assert d.intentionality == "high"
    assert d.intent_confidence == 8.5


def test_attribute_no_unanimity_stores_nothing(mongo_db) -> None:
    seed_intents(mongo_db)
    _seed_entry(mongo_db)
    adapter = _SeqAdapter(responses=[
        _verdict(["teach"], "high", 9.0),
        _verdict(["persuade"], "low", 9.0),   # disjoint tags + ordinal clash
        _verdict(["warn"], "medium", 9.0),
    ])
    a = attribute_intent(mongo_db, "MPL-001", 0, adapter, passes=3, apply=True)
    assert a.outcome == "no_unanimous_tags" and a.stored is False
    d = OntologyEntryRepository(mongo_db).get("MPL-001").definitions[0]
    assert d.intent_tags == [] and d.intentionality is None


def test_attribute_below_threshold_not_stored(mongo_db) -> None:
    seed_intents(mongo_db)
    _seed_entry(mongo_db)
    adapter = _SeqAdapter(responses=[
        _verdict(["teach"], "high", 9.0),
        _verdict(["teach"], "high", 5.0),     # one shaky pass drags the min
        _verdict(["teach"], "high", 9.0),
    ])
    a = attribute_intent(
        mongo_db, "MPL-001", 0, adapter, passes=3,
        min_confidence=8.0, apply=True,
    )
    assert a.outcome == "below_threshold" and a.stored is False
    assert a.unanimous_tags == ["teach"]      # surfaced for operator review
    d = OntologyEntryRepository(mongo_db).get("MPL-001").definitions[0]
    assert d.intent_tags == []


def test_attribute_invented_tag_names_dropped(mongo_db) -> None:
    seed_intents(mongo_db)
    _seed_entry(mongo_db)
    adapter = _SeqAdapter(responses=[_verdict(["evangelize"], "high", 9.0)] * 3)
    a = attribute_intent(mongo_db, "MPL-001", 0, adapter, passes=3, apply=True)
    # Unanimous but not in the taxonomy -> resolves to nothing; the
    # invented name never reaches storage. The unanimous in-vocabulary
    # ordinal is independently storable, so the attribution still lands
    # — with EMPTY tags.
    assert a.unanimous_tag_ids == []
    assert a.outcome == "stored"
    d = OntologyEntryRepository(mongo_db).get("MPL-001").definitions[0]
    assert d.intent_tags == []
    assert d.intentionality == "high"


def test_attribute_invented_tags_and_no_ordinal_stores_nothing(mongo_db) -> None:
    seed_intents(mongo_db)
    _seed_entry(mongo_db)
    # Invented tag AND no unanimous ordinal -> nothing storable at all.
    adapter = _SeqAdapter(responses=[
        _verdict(["evangelize"], "high", 9.0),
        _verdict(["evangelize"], "low", 9.0),
        _verdict(["evangelize"], "high", 9.0),
    ])
    a = attribute_intent(mongo_db, "MPL-001", 0, adapter, passes=3, apply=True)
    assert a.outcome == "no_unanimous_tags" and a.stored is False
    d = OntologyEntryRepository(mongo_db).get("MPL-001").definitions[0]
    assert d.intent_tags == [] and d.intentionality is None


def test_attribute_parse_failure_voids_unanimity(mongo_db) -> None:
    seed_intents(mongo_db)
    _seed_entry(mongo_db)
    adapter = _SeqAdapter(responses=[
        _verdict(["teach"]), "garbled non-json", _verdict(["teach"]),
    ])
    a = attribute_intent(mongo_db, "MPL-001", 0, adapter, passes=3, apply=True)
    assert a.outcome == "parse_failed" and a.stored is False


def test_attribute_intentionality_disagreement_drops_ordinal(mongo_db) -> None:
    seed_intents(mongo_db)
    _seed_entry(mongo_db)
    adapter = _SeqAdapter(responses=[
        _verdict(["teach"], "high", 9.0),
        _verdict(["teach"], "medium", 9.0),
        _verdict(["teach"], "high", 9.0),
    ])
    a = attribute_intent(mongo_db, "MPL-001", 0, adapter, passes=3, apply=True)
    assert a.outcome == "stored"
    assert a.intentionality is None           # ordinal not unanimous
    d = OntologyEntryRepository(mongo_db).get("MPL-001").definitions[0]
    assert d.intent_tags and d.intentionality is None


def test_attribute_no_taxonomy_returns_none(mongo_db) -> None:
    _seed_entry(mongo_db)
    adapter = _SeqAdapter(responses=[_verdict(["teach"])])
    assert attribute_intent(mongo_db, "MPL-001", 0, adapter) is None
    assert adapter.calls == []                # adapter never consulted


def test_backfill_intents_dry_run_and_apply(mongo_db) -> None:
    seed_intents(mongo_db)
    _seed_entry(mongo_db, "MPL-001", "alpha")
    _seed_entry(mongo_db, "MPL-002", "beta")
    adapter = _SeqAdapter(responses=[_verdict(["teach"], "high", 9.0)])

    dry = backfill_intents(mongo_db, adapter, passes=3, apply=False)
    assert dry.unattributed_at_start == 2
    assert dry.stored == 2                    # would-store outcomes
    d = OntologyEntryRepository(mongo_db).get("MPL-001").definitions[0]
    assert d.intent_tags == []                # ...but nothing written

    applied = backfill_intents(mongo_db, adapter, passes=3, apply=True)
    assert applied.stored == 2
    d = OntologyEntryRepository(mongo_db).get("MPL-001").definitions[0]
    assert len(d.intent_tags) == 1

    # Attributed definitions drop out of the next walk.
    again = backfill_intents(mongo_db, adapter, passes=3, apply=True)
    assert again.unattributed_at_start == 0


def test_backfill_intents_only_labels_scoping(mongo_db) -> None:
    seed_intents(mongo_db)
    _seed_entry(mongo_db, "MPL-001", "alpha")
    _seed_entry(mongo_db, "MPL-002", "beta")
    adapter = _SeqAdapter(responses=[_verdict(["teach"], "high", 9.0)])
    result = backfill_intents(
        mongo_db, adapter, passes=3, apply=True, only_labels={"MPL-002"},
    )
    assert result.unattributed_at_start == 1
    assert OntologyEntryRepository(mongo_db).get("MPL-001").definitions[0].intent_tags == []
    assert OntologyEntryRepository(mongo_db).get("MPL-002").definitions[0].intent_tags != []


def test_backfill_intents_no_taxonomy_noop(mongo_db) -> None:
    _seed_entry(mongo_db)

    class ExplodingAdapter:
        def generate(self, *a, **k):  # pragma: no cover - must not run
            raise AssertionError("no taxonomy -> adapter must not be called")

    result = backfill_intents(mongo_db, ExplodingAdapter(), apply=True)
    assert result.unattributed_at_start == 0


# --- I-C: intent on the read surfaces -----------------------------------------


def _tagged_entry(mongo_db, label="MPL-001", term="alpha"):
    """Entry whose definition carries the 'teach' intent + high ordinal."""
    seed_intents(mongo_db)
    teach_id = resolve_intent_tag(mongo_db, "teach")
    OntologyEntryRepository(mongo_db).insert(OntologyEntry(
        mpl_label=label, canonical_term=term, confidence=8.0,
        definitions=[DefinitionVersion(
            text=f"{term} definition.",
            intent_tags=[teach_id],
            intentionality="high",
            intent_confidence=8.5,
        )],
    ))
    return teach_id


def test_search_filters_by_intent_tag(mongo_db) -> None:
    from mahalath.retrieval import Filters, search_terms
    _tagged_entry(mongo_db, "MPL-001", "alpha")
    OntologyEntryRepository(mongo_db).insert(OntologyEntry(
        mpl_label="MPL-002", canonical_term="alphabet", confidence=8.0,
        definitions=[DefinitionVersion(text="alphabet definition.")],
    ))
    hit = search_terms(mongo_db, ["alpha"], filters=Filters(intent_tag="teach"))
    assert [m.mpl_label for m in hit] == ["MPL-001"]
    miss = search_terms(mongo_db, ["alpha"], filters=Filters(intent_tag="warn"))
    assert miss == []
    # Unknown tag (or a frame name) fails closed.
    unknown = search_terms(
        mongo_db, ["alpha"], filters=Filters(intent_tag="nonexistent"),
    )
    assert unknown == []


def test_get_codified_carries_intent_names(mongo_db) -> None:
    from mahalath.retrieval import get_codified
    _tagged_entry(mongo_db)
    ref = get_codified(mongo_db, "MPL-001")
    m = ref.meanings[0]
    assert m.intent_tags == ["teach"]          # readable name, not the id
    assert m.intentionality == "high"
    assert m.intent_confidence == 8.5


def test_bundle_text_shows_deployment_aside(mongo_db) -> None:
    from mahalath.retrieval import build_bundle
    _tagged_entry(mongo_db)
    bundle = build_bundle(mongo_db, ["alpha"])
    assert "(deployed to: teach; intentionality: high)" in bundle.as_text


def test_chat_prompt_shows_deployment_aside(mongo_db) -> None:
    import json as __json
    from mahalath.adapters import MockAdapter
    from mahalath.chat import answer_question

    _tagged_entry(mongo_db)
    captured: dict = {}

    class CapturingAdapter(MockAdapter):
        def generate(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return super().generate(prompt, **kwargs)

    adapter = CapturingAdapter(default_response=__json.dumps(
        {"answer": "ok", "suggested_actions": []}
    ))
    answer_question("what is alpha?", mongo_db, adapter)
    assert "deployed to: teach" in captured["prompt"]


def test_glossary_exports_intent_fields(mongo_db) -> None:
    import json as __json
    from mahalath.glossary import export_json, export_markdown

    teach_id = _tagged_entry(mongo_db)
    payload = __json.loads(export_json(mongo_db, database_name="x").output)
    d = payload["entries"][0]["definitions"][0]
    assert d["intent_tag_ids"] == [teach_id]
    assert d["intent_tags"] == ["teach"]
    assert d["intentionality"] == "high"
    assert d["intent_confidence"] == 8.5

    md = export_markdown(mongo_db, database_name="x").output
    assert "_(deployed to: teach; intentionality: high)_" in md
