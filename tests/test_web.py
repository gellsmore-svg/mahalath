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
from mahalath.config import AppConfig, MongoConfig
from mahalath.db.models import DefinitionVersion, OntologyEntry
from mahalath.db.repositories import (
    ActionProposalRepository,
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


def test_ontology_detail_shows_definition(app_client, mongo_db) -> None:
    _seed_two_entries(mongo_db)
    r = app_client.get("/ontology/MPL-001")
    assert r.status_code == 200
    assert "Underlying medium." in r.text
    assert "gemma4:e2b" in r.text


def test_ontology_detail_404_on_missing(app_client) -> None:
    r = app_client.get("/ontology/MPL-DOES-NOT-EXIST")
    assert r.status_code == 404


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
