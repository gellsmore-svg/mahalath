"""Web UI tests with FastAPI TestClient + the mongo_db fixture.

Covers: dashboard renders; ontology list / detail; proposals list with
filter; accept POST applies the action; reject POST records the
decision; rollback POST reverses an applied action; 404 on unknown
proposal.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from mahalath.actions import ProposeParent, dispatch
from mahalath.config import AppConfig
from mahalath.db.models import DefinitionContext, DefinitionVersion, OntologyEntry
from mahalath.db.repositories import (
    ActionProposalRepository,
    DefinitionContextRepository,
    OntologyEntryRepository,
)
from mahalath.web.app import create_app


@pytest.fixture
def app_client(mongo_db, mongo_config: AppConfig):
    # Bind the app to the test database
    app = create_app(mongo_config)
    with TestClient(app) as client:
        yield client


def _seed_two_entries(mongo_db) -> None:
    repo = OntologyEntryRepository(mongo_db)
    repo.insert(OntologyEntry(
        mpl_label="MPL-001", canonical_term="Substrate", confidence=8.0,
        definitions=[DefinitionVersion(text="Underlying medium.", model_used="gemma4:e2b")],
    ))
    repo.insert(OntologyEntry(
        mpl_label="MPL-002", canonical_term="Relational Substrate",
        confidence=8.5,
        definitions=[DefinitionVersion(text="Relational variant.", model_used="gemma4:e2b")],
    ))


def test_dashboard_renders_counts(app_client, mongo_db) -> None:
    _seed_two_entries(mongo_db)
    r = app_client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "Mahalath" in body
    assert "2" in body  # 2 ontology entries
    assert "mahalath_pytest" in body  # database name


def test_ontology_list_shows_entries(app_client, mongo_db) -> None:
    _seed_two_entries(mongo_db)
    r = app_client.get("/ontology")
    assert r.status_code == 200
    assert "MPL-001" in r.text
    assert "Substrate" in r.text
    assert "MPL-002" in r.text
    assert "Relational Substrate" in r.text


def test_ontology_list_badges_untagged_entries(app_client, mongo_db) -> None:
    ctx = DefinitionContext(name="structural", description="Structural frame.")
    DefinitionContextRepository(mongo_db).insert(ctx)
    repo = OntologyEntryRepository(mongo_db)
    # MPL-001: one untagged definition → should show an "untagged 1" badge.
    repo.insert(OntologyEntry(
        mpl_label="MPL-001", canonical_term="alpha", confidence=8.0,
        definitions=[DefinitionVersion(text="Untagged.", model_used="operator")],
    ))
    # MPL-002: fully tagged → no badge.
    repo.insert(OntologyEntry(
        mpl_label="MPL-002", canonical_term="beta", confidence=8.0,
        definitions=[DefinitionVersion(text="Tagged.", model_used="gemma4:e2b",
                                       context_id=ctx.context_id)],
    ))
    r = app_client.get("/ontology")
    assert r.status_code == 200
    assert "untagged 1" in r.text
    assert "1 with untagged definitions" in r.text


def test_ontology_detail_shows_definition(app_client, mongo_db) -> None:
    _seed_two_entries(mongo_db)
    r = app_client.get("/ontology/MPL-001")
    assert r.status_code == 200
    assert "Underlying medium." in r.text
    assert "gemma4:e2b" in r.text


def test_ontology_detail_404_on_missing(app_client) -> None:
    r = app_client.get("/ontology/MPL-DOES-NOT-EXIST")
    assert r.status_code == 404


def test_ontology_detail_groups_definitions_by_context(app_client, mongo_db) -> None:
    ctx_repo = DefinitionContextRepository(mongo_db)
    ctx_st = DefinitionContext(name="structural", description="Generic structural frame.")
    ctx_th = DefinitionContext(name="theological", description="Biblical creation frame.")
    ctx_repo.insert(ctx_st)
    ctx_repo.insert(ctx_th)
    OntologyEntryRepository(mongo_db).insert(OntologyEntry(
        mpl_label="MPL-004", canonical_term="substrate", confidence=8.0,
        definitions=[
            DefinitionVersion(text="The generic underlying medium.",
                              model_used="gemma4:e2b", context_id=ctx_st.context_id),
            DefinitionVersion(text="The grammatical mechanism of creaturely existence.",
                              model_used="operator", context_id=ctx_th.context_id),
        ],
    ))
    r = app_client.get("/ontology/MPL-004")
    assert r.status_code == 200
    body = r.text
    # Both frame badges + descriptions present, both definitions present.
    assert "structural" in body and "theological" in body
    assert "Generic structural frame." in body
    assert "Biblical creation frame." in body
    assert "generic underlying medium" in body
    assert "grammatical mechanism of creaturely existence" in body
    # Polysemy note appears for a multi-frame term.
    assert "co-equal frames" in body


def test_ontology_detail_badges_untagged_definition(app_client, mongo_db) -> None:
    OntologyEntryRepository(mongo_db).insert(OntologyEntry(
        mpl_label="MPL-001", canonical_term="alpha", confidence=8.0,
        definitions=[DefinitionVersion(text="An untagged definition.", model_used="operator")],
    ))
    r = app_client.get("/ontology/MPL-001")
    assert r.status_code == 200
    assert "untagged" in r.text
    assert "An untagged definition." in r.text
    # Single frame (none) → no polysemy note.
    assert "co-equal frames" not in r.text


def test_proposals_list_filter_by_status(app_client, mongo_db) -> None:
    _seed_two_entries(mongo_db)
    # Create one pending proposal
    dispatch(
        ProposeParent(
            child_label="MPL-002", parent_label="MPL-001",
            reason="test", confidence=7.0,
        ),
        mongo_db,
    )
    r = app_client.get("/proposals?status=pending_review")
    assert r.status_code == 200
    assert "MPL-002" in r.text
    assert "MPL-001" in r.text
    assert "pending_review" in r.text


def test_proposal_accept_round_trip(app_client, mongo_db) -> None:
    _seed_two_entries(mongo_db)
    proposal = dispatch(
        ProposeParent(
            child_label="MPL-002", parent_label="MPL-001",
            reason="test", confidence=7.0,
        ),
        mongo_db,
    )
    # Accept via POST
    r = app_client.post(
        f"/proposals/{proposal.proposal_id}/accept",
        data={"note": "manually verified"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/proposals/{proposal.proposal_id}"

    # Underlying ontology now has MPL-002 as a child of MPL-001
    fetched = OntologyEntryRepository(mongo_db).get("MPL-002")
    assert fetched.parent_label == "MPL-001"
    stored_proposal = ActionProposalRepository(mongo_db).get(proposal.proposal_id)
    assert stored_proposal.status == "applied"
    assert stored_proposal.operator_decision == "accepted"
    assert stored_proposal.operator_note == "manually verified"


def test_proposal_reject_records_decision(app_client, mongo_db) -> None:
    _seed_two_entries(mongo_db)
    proposal = dispatch(
        ProposeParent(
            child_label="MPL-002", parent_label="MPL-001",
            reason="test", confidence=7.0,
        ),
        mongo_db,
    )
    r = app_client.post(
        f"/proposals/{proposal.proposal_id}/reject",
        data={"note": "wrong direction"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    stored = ActionProposalRepository(mongo_db).get(proposal.proposal_id)
    assert stored.status == "rejected"
    assert stored.operator_note == "wrong direction"


def test_proposal_detail_404_on_missing(app_client) -> None:
    r = app_client.get("/proposals/no-such-id")
    assert r.status_code == 404


def test_undecided_renders_empty_state(app_client) -> None:
    r = app_client.get("/undecided")
    assert r.status_code == 200
    assert "queue is empty" in r.text


def test_documents_renders_empty_state(app_client) -> None:
    r = app_client.get("/documents")
    assert r.status_code == 200
    assert "no documents" in r.text


# --- /api/retrieve (S-C) ----------------------------------------------------


def _seed_retrieval_chain(mongo_db) -> None:
    repo = OntologyEntryRepository(mongo_db)
    repo.insert(OntologyEntry(
        mpl_label="MPL-001", canonical_term="alpha", confidence=8.0,
        references_labels=["MPL-002"],
        definitions=[DefinitionVersion(text="Alpha cites MPL-002.")],
    ))
    repo.insert(OntologyEntry(
        mpl_label="MPL-002", canonical_term="beta", confidence=8.0,
        definitions=[DefinitionVersion(text="Beta meaning.")],
    ))


def test_api_retrieve_returns_bundle(app_client, mongo_db) -> None:
    _seed_retrieval_chain(mongo_db)
    r = app_client.post("/api/retrieve", json={"terms": ["alpha"]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    bundle = body["bundle"]
    assert bundle["entries"][0]["mpl_label"] == "MPL-001"
    # Reference closure (ADR-023) rides along.
    assert [c["mpl_label"] for c in bundle["closure"]] == ["MPL-002"]
    assert "CODIFIED MEANINGS" in bundle["as_text"]


def test_api_retrieve_text_format(app_client, mongo_db) -> None:
    _seed_retrieval_chain(mongo_db)
    r = app_client.post(
        "/api/retrieve", json={"terms": ["alpha"], "format": "text"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "bundle" not in body
    assert "--- MPL-001: alpha ---" in body["text"]


def test_api_retrieve_accepts_labels_and_filters(app_client, mongo_db) -> None:
    _seed_retrieval_chain(mongo_db)
    r = app_client.post("/api/retrieve", json={
        "labels": ["MPL-002"],
        "filters": {"min_confidence": 5.0},
        "token_budget": 800,
    })
    assert r.status_code == 200
    bundle = r.json()["bundle"]
    assert bundle["entries"][0]["mpl_label"] == "MPL-002"
    assert bundle["token_budget"] == 800


def test_api_retrieve_requires_input(app_client) -> None:
    r = app_client.post("/api/retrieve", json={})
    assert r.status_code == 400


# --- /api/propose_term (S-D) -------------------------------------------------


def test_api_propose_term_enqueues(app_client, mongo_db) -> None:
    r = app_client.post("/api/propose_term", json={
        "term": "morphogenesis",
        "context": "Morphogenesis is the emergence of stable form.",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "enqueued"
    assert mongo_db.undecided_queue.count_documents(
        {"term": "morphogenesis", "reason": "proposed_term"}
    ) == 1


def test_api_propose_term_existing_match(app_client, mongo_db) -> None:
    _seed_retrieval_chain(mongo_db)
    r = app_client.post("/api/propose_term", json={"term": "alpha"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "existing"
    assert body["matches"][0]["mpl_label"] == "MPL-001"
    assert mongo_db.undecided_queue.count_documents({}) == 0


def test_api_propose_term_dry_run(app_client, mongo_db) -> None:
    r = app_client.post("/api/propose_term", json={
        "term": "morphogenesis", "dry_run": True,
    })
    assert r.status_code == 200
    assert r.json()["status"] == "template_only"
    assert mongo_db.undecided_queue.count_documents({}) == 0


def test_api_propose_term_requires_term(app_client) -> None:
    r = app_client.post("/api/propose_term", json={})
    assert r.status_code == 400


# --- intent badges on the detail page (I-C) -----------------------------------


def test_detail_page_shows_intent_badges(app_client, mongo_db) -> None:
    from mahalath.intents import resolve_intent_tag, seed_intents

    seed_intents(mongo_db)
    teach_id = resolve_intent_tag(mongo_db, "teach")
    OntologyEntryRepository(mongo_db).insert(OntologyEntry(
        mpl_label="MPL-001", canonical_term="alpha", confidence=8.0,
        definitions=[DefinitionVersion(
            text="Alpha definition.",
            intent_tags=[teach_id],
            intentionality="high",
            intent_confidence=8.5,
        )],
    ))
    r = app_client.get("/ontology/MPL-001")
    assert r.status_code == 200
    assert '<span class="badge intent">teach</span>' in r.text
    assert "intentionality: high" in r.text


def test_detail_page_no_intent_badges_when_unannotated(app_client, mongo_db) -> None:
    _seed_two_entries(mongo_db)
    r = app_client.get("/ontology/MPL-001")
    assert r.status_code == 200
    assert 'badge intent' not in r.text


# --- decision-effectiveness page + API (§3.4) ---------------------------------


def test_effectiveness_page_renders(app_client, mongo_db) -> None:
    from mahalath.db.models import ActionProposal
    from mahalath.db.repositories import ActionProposalRepository

    _seed_two_entries(mongo_db)
    ActionProposalRepository(mongo_db).insert(ActionProposal(
        action_type="propose_parent", confidence=9.0,
        status="pending_review", operator_decision="accepted",
    ))
    r = app_client.get("/effectiveness")
    assert r.status_code == 200
    assert "Decision effectiveness" in r.text
    assert "Findings" in r.text
    assert "Calibration" in r.text


def test_effectiveness_page_empty_db(app_client) -> None:
    r = app_client.get("/effectiveness")
    assert r.status_code == 200
    assert "not enough operator decisions" in r.text


def test_api_effectiveness_returns_report(app_client, mongo_db) -> None:
    _seed_two_entries(mongo_db)
    r = app_client.get("/api/effectiveness")
    assert r.status_code == 200
    payload = r.json()
    assert payload["ok"] is True
    report = payload["report"]
    assert report["coverage"]["entries"] == 2
    assert "findings" in report and report["findings"]


def test_chat_page_escapes_model_output_paths(app_client) -> None:
    """The chat page ships an esc() helper and applies it to every dynamic
    interpolation (answer, reasoning, labels, errors) before innerHTML."""
    page = app_client.get("/chat").text
    assert "function esc(" in page
    assert "esc(data.answer)" in page
    assert "esc(a.reasoning)" in page
    assert "esc(data.detail || 'error')" in page


def test_chat_apply_action_clamps_confidence(app_client, monkeypatch) -> None:
    captured = {}

    class FakeResult:
        proposal_id = "p1"
        status = "pending_review"
        detail = "queued"
        payload = {}

    def fake_dispatch(action, db):
        captured["confidence"] = action.confidence
        return FakeResult()

    import mahalath.actions as actions
    monkeypatch.setattr(actions, "dispatch", fake_dispatch)
    response = app_client.post("/api/chat/apply_action", json={
        "type": "propose_alias",
        "payload": {"label": "MPL-001", "alias": "x"},
        "confidence": 999.0,
        "reasoning": "r",
    })
    assert response.status_code == 200
    assert captured["confidence"] <= 10.0
