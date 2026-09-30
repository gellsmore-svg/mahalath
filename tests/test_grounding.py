"""Semantic grounding (ADR-038).

The source document stays primary. These tests cover the decision
rules, the three modes, the retrievers, persistence, and the surfaces
that must not reprint external passages.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from mahalath.adapters.mock import MockAdapter
from mahalath.config import (
    AgentRoleConfig,
    AgentRolesConfig,
    RuntimeConfig,
    SemanticGroundingConfig,
    load_config,
)
from mahalath.db.models import AgentExchange, DefinitionVersion, OntologyEntry
from mahalath.db.repositories import (
    OntologyEntryRepository,
    SemanticGroundingRepository,
)
from mahalath.debate import DebateResult
from mahalath.glossary import _render_definition
from mahalath.grounding import (
    EvidenceHit,
    GroundingOpinion,
    apply_grounding,
    build_retriever,
    decide_grounding,
    grounding_summary_line,
    lookup_required,
    parse_wikipedia_search,
)
from mahalath.ontology import persist_debate_result, redebate_entry
from mahalath.retrieval import Meaning, get_codified, render_entry_lines


CORPUS = "In this document a vorton is a stable relational knot."
REFINED = (
    "In this document a vorton is a stable relational knot "
    "the author treats as one object."
)
PASSAGE = (
    "Elsewhere a vorton is a vortex loop in a cosmological field model "
    "with no bearing on this author's wording."
)


def _opinion(
    *,
    relation: str = "enrich",
    definition: str = REFINED,
    confidence: float = 9.0,
    faithful: bool = True,
) -> GroundingOpinion:
    return GroundingOpinion(
        definition=definition,
        explanation="checked against the source",
        confidence=confidence,
        relation=relation,
        source_faithful=faithful,
        evidence_notes="",
    )


def _hits() -> list[EvidenceHit]:
    return [EvidenceHit(
        provider="static", title="Vorton", snippet=PASSAGE,
        url="https://example.invalid/vorton",
    )]


def _result(
    *,
    definition: str | None = CORPUS,
    confidence: float | None = 6.5,
    outcome: str = "undecided",
) -> DebateResult:
    return DebateResult(
        decision_log_id="dl-ground-1",
        term="vorton",
        source_document_id="doc-1",
        outcome=outcome,
        final_definition=definition,
        final_confidence=confidence,
        iterations_used=1,
        context="The author calls this stable relational knot a vorton.",
    )


def _runtime() -> RuntimeConfig:
    return RuntimeConfig(
        confidence_threshold=8.0,
        max_iterations_per_term=2,
        generate_detailed_definitions=False,
        model="mock-model",
    )


def test_lookup_required_follows_the_three_modes() -> None:
    assert lookup_required("corpus_only", 1.0, 8.0) is False
    assert lookup_required("hybrid", 8.0, 8.0) is False
    assert lookup_required("hybrid", 7.9, 8.0) is True
    assert lookup_required("hybrid", None, 8.0) is True
    assert lookup_required("external", 9.5, 8.0) is True
    with pytest.raises(ValueError):
        lookup_required("web", 1.0, 8.0)


def test_challenge_keeps_corpus_wording_and_cannot_raise_confidence() -> None:
    decision = decide_grounding(
        corpus_definition=CORPUS,
        corpus_confidence=7.5,
        critic=_opinion(relation="challenge", confidence=9.5, definition="Outside sense."),
        explorer=_opinion(relation="corroborate", confidence=9.0),
        hits=_hits(),
    )
    assert decision.relation == "challenge"
    assert decision.adopted is False
    assert decision.challenged is True
    assert decision.definition == CORPUS
    assert decision.confidence == 7.5


def test_revision_is_adopted_only_when_it_clears_the_corpus_floor() -> None:
    adopted = decide_grounding(
        corpus_definition=CORPUS,
        corpus_confidence=6.5,
        critic=_opinion(),
        explorer=_opinion(),
        hits=_hits(),
    )
    assert adopted.adopted is True
    assert adopted.definition == REFINED
    assert adopted.confidence == 9.0

    timid = decide_grounding(
        corpus_definition=CORPUS,
        corpus_confidence=8.5,
        critic=_opinion(confidence=8.0),
        explorer=_opinion(confidence=8.4),
        hits=_hits(),
    )
    assert timid.adopted is False
    assert timid.definition == CORPUS
    assert timid.confidence == 8.5


def test_verbatim_external_wording_is_not_stored() -> None:
    decision = decide_grounding(
        corpus_definition=CORPUS,
        corpus_confidence=6.0,
        critic=_opinion(definition=PASSAGE),
        explorer=_opinion(definition=PASSAGE),
        hits=_hits(),
    )
    assert decision.adopted is False
    assert decision.definition == CORPUS
    assert "verbatim" in (decision.note or "")


@pytest.mark.parametrize(
    ("passage", "candidate"),
    [
        ("Compact outside definition.", "Compact outside definition."),
        (
            "External evidence gives this complete source passage in full.",
            "Definition: External evidence gives this complete source passage in full.",
        ),
        (
            "External evidence gives this complete source passage in full.",
            "External evidence gives this complete source passage in full. Added gloss.",
        ),
        (
            "one two three four five six seven eight nine ten",
            "A source-aware definition uses three four five six seven eight nine ten here.",
        ),
    ],
)
def test_short_wrapped_and_partial_external_copies_are_not_stored(
    passage: str, candidate: str,
) -> None:
    hits = [EvidenceHit(provider="test", title="Source", snippet=passage)]
    decision = decide_grounding(
        corpus_definition=CORPUS,
        corpus_confidence=6.0,
        critic=_opinion(definition=candidate),
        explorer=_opinion(definition=candidate),
        hits=hits,
    )
    assert decision.adopted is False
    assert decision.definition == CORPUS
    assert "verbatim" in (decision.note or "")


def test_shared_term_of_art_does_not_count_as_a_verbatim_copy() -> None:
    candidate = (
        "Here a stable relational knot names one persistent object in the argument."
    )
    hits = [EvidenceHit(
        provider="test",
        title="Stable relational knot",
        snippet=(
            "A stable relational knot can also describe a different mathematical "
            "construction in unrelated literature."
        ),
    )]
    decision = decide_grounding(
        corpus_definition=CORPUS,
        corpus_confidence=6.0,
        critic=_opinion(definition=candidate),
        explorer=_opinion(definition=candidate),
        hits=hits,
    )
    assert decision.adopted is True
    assert decision.definition == candidate


def test_explorer_revision_requires_exact_final_ratification() -> None:
    explorer_definition = (
        "In this document a vorton is a stable relational knot with extra force."
    )
    critic_definition = (
        "In this document a vorton is merely a stable relational knot."
    )
    decision = decide_grounding(
        corpus_definition=CORPUS,
        corpus_confidence=6.0,
        initial_critic=_opinion(definition=critic_definition),
        explorer=_opinion(definition=explorer_definition),
        critic=_opinion(definition=critic_definition),
        hits=_hits(),
    )
    assert decision.adopted is False
    assert decision.definition == CORPUS
    assert "not ratified" in (decision.note or "")


def test_no_corpus_definition_is_not_filled_from_outside() -> None:
    decision = decide_grounding(
        corpus_definition=None,
        corpus_confidence=None,
        critic=_opinion(),
        explorer=_opinion(),
        hits=_hits(),
    )
    assert decision.adopted is False
    assert decision.definition is None


def test_corpus_only_never_calls_the_retriever() -> None:
    class Boom:
        def retrieve(self, query: str, *, limit: int):
            raise AssertionError("corpus_only must not retrieve")

    result = apply_grounding(
        _result(confidence=4.0),
        SemanticGroundingConfig(mode="corpus_only", providers=["wikipedia"]),
        _runtime(),
        adapter=MockAdapter(),
        retriever=Boom(),
    )
    assert result.grounding_mode == "corpus_only"
    assert result.grounding_invoked is False
    assert result.final_definition == CORPUS
    assert result.grounding_hits == []


def test_hybrid_retrieves_only_below_the_threshold() -> None:
    calls = {"n": 0}

    class Counting:
        errors: list[str] = []

        def retrieve(self, query: str, *, limit: int):
            calls["n"] += 1
            return _hits()

    high = apply_grounding(
        _result(confidence=8.0, outcome="accepted"),
        SemanticGroundingConfig(mode="hybrid", external_lookup_threshold=8.0),
        _runtime(),
        retriever=Counting(),
    )
    assert high.grounding_invoked is False
    assert calls["n"] == 0

    low = apply_grounding(
        _result(confidence=7.9),
        SemanticGroundingConfig(mode="hybrid", external_lookup_threshold=8.0),
        _runtime(),
        adapter=None,
        retriever=Counting(),
    )
    assert low.grounding_invoked is True
    assert calls["n"] == 1
    assert low.grounding_adopted is False
    assert "no model" in (low.grounding_note or "")
    assert low.final_definition == CORPUS


def test_retrieval_failure_keeps_the_corpus_decision() -> None:
    class Down:
        def retrieve(self, query: str, *, limit: int):
            raise RuntimeError("network down")

    result = apply_grounding(
        _result(confidence=6.0, outcome="undecided"),
        SemanticGroundingConfig(mode="external"),
        _runtime(),
        retriever=Down(),
    )
    assert result.grounding_invoked is True
    assert result.outcome == "undecided"
    assert result.final_definition == CORPUS
    assert result.final_confidence == 6.0
    assert result.grounding_note.startswith("retrieval failed:")


def test_grounding_round_adopts_a_source_faithful_revision() -> None:
    payload = json.dumps({
        "relation": "enrich",
        "source_faithful": True,
        "definition": REFINED,
        "explanation": "same sense, tighter wording",
        "confidence": 9.0,
        "evidence_notes": "the passage does not replace the document",
    })
    adapter = MockAdapter(responses={
        "[Grounding speaker: precision_critic]": payload,
        "[Grounding speaker: synthesis_explorer]": payload,
        "[Grounding ratifier: precision_critic]": payload,
    })
    result = apply_grounding(
        _result(confidence=6.5),
        SemanticGroundingConfig(mode="hybrid", external_lookup_threshold=8.0),
        _runtime(),
        adapter=adapter,
        retriever=type("R", (), {"retrieve": lambda self, query, limit: _hits(), "errors": []})(),
    )
    assert result.outcome == "accepted"
    assert result.grounding_adopted is True
    assert result.final_definition == REFINED
    assert result.final_confidence == 9.0
    assert result.corpus_confidence == 6.5
    assert len(result.exchanges) == 3
    assert [exchange.role for exchange in result.exchanges] == [
        "grounding_precision_critic",
        "grounding_synthesis_explorer",
        "grounding_precision_critic_ratification",
    ]
    assert "https://example.invalid/vorton" in result.exchanges[0].prompt
    assert "[Speaker: precision_critic]" not in result.exchanges[0].prompt
    assert "FINAL CANDIDATE TO RATIFY" in result.exchanges[-1].prompt
    assert REFINED in result.exchanges[-1].prompt


def test_failed_grounding_call_is_audited_and_keeps_corpus_author(mongo_db) -> None:
    result = _result(confidence=8.5, outcome="accepted")
    result.exchanges.extend([
        AgentExchange(
            decision_log_id=result.decision_log_id,
            iteration=1,
            role="precision_critic",
            model="corpus-critic-model",
            prompt="corpus critic prompt",
            response="corpus critic response",
            confidence=9.0,
        ),
        AgentExchange(
            decision_log_id=result.decision_log_id,
            iteration=1,
            role="synthesis_explorer",
            model="corpus-explorer-model",
            prompt="corpus explorer prompt",
            response="corpus explorer response",
            confidence=8.5,
        ),
    ])
    grounding_payload = json.dumps({
        "relation": "enrich",
        "source_faithful": True,
        "definition": REFINED,
        "explanation": "candidate",
        "confidence": 9.0,
        "evidence_notes": "",
    })
    runtime = _runtime().model_copy(update={
        "agents": AgentRolesConfig(
            precision_critic=AgentRoleConfig(model="grounding-critic-model"),
            synthesis_explorer=AgentRoleConfig(model="grounding-explorer-model"),
        ),
    })
    adapter = MockAdapter(responses={
        "[Grounding speaker: precision_critic]": grounding_payload,
        "[Grounding speaker: synthesis_explorer]": "not JSON",
    })
    grounded = apply_grounding(
        result,
        SemanticGroundingConfig(mode="external"),
        runtime,
        adapter=adapter,
        retriever=type(
            "R", (),
            {"retrieve": lambda self, query, limit: _hits(), "errors": []},
        )(),
    )
    assert grounded.final_definition == CORPUS
    assert grounded.grounding_adopted is False
    assert grounded.grounding_note.startswith("grounding model call failed:")
    failed = grounded.exchanges[-1]
    assert failed.role == "grounding_synthesis_explorer"
    assert failed.model == "grounding-explorer-model"
    assert failed.response == "not JSON"
    assert failed.confidence is None

    persisted = persist_debate_result(grounded, mongo_db, runtime)
    stored = persisted.ontology_entry.definitions[0]
    assert stored.model_used == "corpus-explorer-model"
    log = mongo_db.decision_log.find_one(
        {"decision_log_id": grounded.decision_log_id}
    )
    assert log["iterations_used"] == 2
    raw = mongo_db.agent_exchanges.find_one({
        "decision_log_id": grounded.decision_log_id,
        "role": "grounding_synthesis_explorer",
    })
    assert raw["response"] == "not JSON"


def test_static_wikipedia_and_http_retrievers(tmp_path: Path) -> None:
    path = tmp_path / "passages.json"
    path.write_text(json.dumps({
        "hits": [
            {"term": "vorton", "title": "Local", "snippet": PASSAGE,
             "url": "https://example.invalid/local"},
            {"term": "other", "title": "Skip", "snippet": "not this term"},
        ],
    }), encoding="utf-8")
    static = build_retriever(SemanticGroundingConfig(
        mode="external", providers=["static"], static_path=str(path),
    ))
    assert static is not None
    found = static.retrieve("vorton", limit=5)
    assert [hit.title for hit in found] == ["Local"]

    wiki = parse_wikipedia_search({
        "query": {"search": [{
            "title": "Vorton",
            "snippet": "A <span class=\"searchmatch\">vorton</span> &amp; field.",
        }]},
    }, language="en")
    assert wiki[0].url == "https://en.wikipedia.org/wiki/Vorton"
    assert "<span" not in wiki[0].snippet
    assert "&amp;" not in wiki[0].snippet

    def opener(url: str, *, timeout: float, user_agent: str) -> str:
        assert "q=vorton" in url
        assert "Mahalath/" in user_agent
        return json.dumps({"hits": [{
            "title": "Remote", "snippet": "remote passage",
            "url": "javascript:alert(1)",
        }]})

    http = build_retriever(
        SemanticGroundingConfig(
            mode="external", providers=["http"],
            http_url="https://example.invalid/ground",
        ),
        opener=opener,
    )
    assert http is not None
    remote = http.retrieve("vorton", limit=3)
    assert remote[0].title == "Remote"
    assert remote[0].url is None


def test_config_rejects_a_threshold_off_the_confidence_scale() -> None:
    with pytest.raises(ValidationError):
        SemanticGroundingConfig(external_lookup_threshold=11)
    with pytest.raises(ValidationError):
        SemanticGroundingConfig(mode="always")


def test_env_overrides_mode_and_threshold(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "semantic_grounding:\n  mode: corpus_only\n", encoding="utf-8",
    )
    monkeypatch.setenv("MAHALATH_GROUNDING_MODE", "hybrid")
    monkeypatch.setenv("MAHALATH_GROUNDING_THRESHOLD", "6.5")
    loaded = load_config()
    assert loaded.semantic_grounding.mode == "hybrid"
    assert loaded.semantic_grounding.external_lookup_threshold == 6.5


def test_example_config_stays_corpus_only() -> None:
    loaded = load_config(Path(__file__).resolve().parents[1] / "config.example.yaml")
    assert loaded.semantic_grounding.mode == "corpus_only"
    assert loaded.semantic_grounding.providers == []
    assert loaded.semantic_grounding.overnight is False


def test_overnight_defaults_off_and_env_toggles_it(tmp_path: Path, monkeypatch) -> None:
    assert SemanticGroundingConfig().overnight is False
    with pytest.raises(ValidationError):
        SemanticGroundingConfig(overnight="maybe")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MAHALATH_GROUNDING_OVERNIGHT", "true")
    assert load_config().semantic_grounding.overnight is True
    monkeypatch.setenv("MAHALATH_GROUNDING_OVERNIGHT", "false")
    assert load_config().semantic_grounding.overnight is False


def test_summary_line_omits_passages() -> None:
    line = grounding_summary_line(
        invoked=True, relation="corroborate",
        corpus_confidence=6.5, evidence_count=2,
    )
    assert line == (
        "external grounding: corroborate; corpus confidence 6.5; 2 sources"
    )
    assert grounding_summary_line(
        invoked=False, relation=None, corpus_confidence=None, evidence_count=0,
    ) is None


def test_persist_records_mode_without_an_evidence_row_when_not_invoked(mongo_db) -> None:
    result = apply_grounding(
        _result(confidence=9.0, outcome="accepted"),
        SemanticGroundingConfig(),
        _runtime(),
    )
    persisted = persist_debate_result(result, mongo_db, _runtime())
    stored = persisted.ontology_entry.definitions[0]
    assert stored.grounding_mode == "corpus_only"
    assert stored.grounding_invoked is False
    assert SemanticGroundingRepository(mongo_db).for_decision(result.decision_log_id) is None


def test_persist_and_redebate_keep_evidence_on_the_definition(mongo_db, tmp_path: Path) -> None:
    path = tmp_path / "passages.json"
    path.write_text(json.dumps({
        "vorton": [{"title": "Vorton", "snippet": PASSAGE,
                    "url": "https://example.invalid/vorton"}],
    }), encoding="utf-8")
    payload = json.dumps({
        "relation": "enrich",
        "source_faithful": True,
        "definition": REFINED,
        "explanation": "refined from the source",
        "confidence": 9.0,
        "evidence_notes": "",
    })
    corpus_payload_critic = json.dumps({
        "definition": CORPUS, "critique": "sharp", "confidence": 9.0,
    })
    corpus_payload_explorer = json.dumps({
        "definition": CORPUS, "rationale": "kept", "confidence": 9.0,
    })
    adapter = MockAdapter(responses={
        "[Grounding speaker: precision_critic]": payload,
        "[Grounding speaker: synthesis_explorer]": payload,
        "[Grounding ratifier: precision_critic]": payload,
        "[Speaker: precision_critic]": corpus_payload_critic,
        "[Speaker: synthesis_explorer]": corpus_payload_explorer,
    })
    grounding = SemanticGroundingConfig(
        mode="external", providers=["static"], static_path=str(path),
    )
    seeded = persist_debate_result(
        _result(confidence=9.0, outcome="accepted"), mongo_db, _runtime(),
    )
    refreshed = redebate_entry(
        mongo_db, seeded.mpl_label, "The author names the knot a vorton.",
        adapter, _runtime(), apply=True, grounding=grounding,
    )
    assert refreshed.applied is True
    assert refreshed.grounding_invoked is True
    entry = OntologyEntryRepository(mongo_db).get(seeded.mpl_label)
    latest = entry.definitions[-1]
    assert latest.grounding_mode == "external"
    assert latest.grounding_invoked is True
    assert latest.grounding_relation == "enrich"
    assert latest.text == REFINED
    assert latest.external_evidence[0]["url"] == "https://example.invalid/vorton"
    audit = SemanticGroundingRepository(mongo_db).for_decision(
        # The redebate writes its own decision id; the latest definition points at it.
        latest.decision_log_id
    )
    assert audit is not None
    assert audit.adopted is True
    assert audit.mpl_label == seeded.mpl_label
    log = mongo_db.decision_log.find_one({
        "decision_log_id": latest.decision_log_id,
    })
    assert log["iterations_used"] == 2
    assert max(message["iteration"] for message in log["messages"]) == 2

    codified = get_codified(mongo_db, seeded.mpl_label)
    assert codified.meanings[-1].grounding_relation == "enrich"
    rendered = "\n".join(render_entry_lines(
        mpl_label=seeded.mpl_label,
        canonical_term="vorton",
        meanings=[Meaning(
            context_id=None, context_name=None, description=latest.text,
            model_used=None, consensus_score=None, created_at=None,
            grounding_invoked=True, grounding_relation="enrich",
            corpus_confidence=9.0, external_evidence_count=1,
        )],
    ))
    assert "external grounding: enrich" in rendered
    assert "example.invalid" not in rendered


def test_glossary_and_entry_page_do_not_reprint_passages(mongo_db) -> None:
    snippet = '<script>alert(1)</script> https://evil.example/payload'
    OntologyEntryRepository(mongo_db).insert(OntologyEntry(
        mpl_label="MPL-001",
        canonical_term="vorton",
        confidence=9.0,
        definitions=[DefinitionVersion(
            text=CORPUS,
            model_used="mock-model",
            grounding_mode="hybrid",
            grounding_invoked=True,
            grounding_relation="corroborate",
            corpus_confidence=6.5,
            external_evidence=[{
                "provider": "http",
                "title": "Bad",
                "snippet": snippet,
                "url": "https://evil.example/payload",
            }],
        )],
    ))
    glossary = "\n".join(_render_definition(
        OntologyEntryRepository(mongo_db).get("MPL-001").definitions[0]
    ))
    assert "external grounding: corroborate; corpus confidence 6.5; 1 source" in glossary
    assert "<script>" not in glossary
    assert "evil.example" not in glossary


def test_entry_page_does_not_reprint_passages(mongo_db) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from mahalath.config import AppConfig, MongoConfig
    from mahalath.web.app import create_app

    snippet = '<script>alert(1)</script> https://evil.example/payload'
    OntologyEntryRepository(mongo_db).insert(OntologyEntry(
        mpl_label="MPL-001",
        canonical_term="vorton",
        confidence=9.0,
        definitions=[DefinitionVersion(
            text=CORPUS,
            model_used="mock-model",
            grounding_mode="hybrid",
            grounding_invoked=True,
            grounding_relation="corroborate",
            corpus_confidence=6.5,
            external_evidence=[{
                "provider": "http",
                "title": "Bad",
                "snippet": snippet,
                "url": "https://evil.example/payload",
            }],
        )],
    ))
    app = create_app(AppConfig(mongo=MongoConfig(database=mongo_db.name)))
    with TestClient(app) as client:
        page = client.get("/ontology/MPL-001")
    assert page.status_code == 200
    assert "external grounding: corroborate" in page.text
    assert "<script>alert" not in page.text
    assert "evil.example" not in page.text
