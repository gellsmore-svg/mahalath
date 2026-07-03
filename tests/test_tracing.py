"""Galeed pipeline tracing (mahalath.tracing) — optional, best-effort spine witness."""

from __future__ import annotations

import pytest

pytest.importorskip("galeed", reason="galeed extra not installed")

from mahalath.config import AppConfig
from mahalath.tracing import (
    DEBATE_COMPLETED,
    PROPOSAL_ACCEPTED,
    Witness,
    get_witness,
    set_witness,
)


class FakeCollection:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def insert_one(self, doc: dict) -> None:
        self.rows.append(doc)


class FakeDb:
    def __init__(self) -> None:
        self.collections: dict[str, FakeCollection] = {}

    def __getitem__(self, name: str) -> FakeCollection:
        return self.collections.setdefault(name, FakeCollection())


def _enabled_config() -> AppConfig:
    return AppConfig.model_validate({"galeed": {"enabled": True}})


def _events(fake: FakeDb) -> list[dict]:
    return fake.collections.get("trace_events", FakeCollection()).rows


@pytest.fixture(autouse=True)
def _reset_witness():
    yield
    set_witness(None)


def test_disabled_by_default_emits_nothing() -> None:
    fake = FakeDb()
    witness = Witness(AppConfig(), db=fake)
    witness.emit(DEBATE_COMPLETED, trace_id="t1", summary="quiet")
    assert _events(fake) == []


def test_enabled_witness_persists_event_with_source() -> None:
    fake = FakeDb()
    witness = Witness(_enabled_config(), db=fake)
    witness.emit(
        PROPOSAL_ACCEPTED,
        trace_id="prop-1",
        summary="accepted propose_parent proposal",
        proposal_id="prop-1",
        action_type="propose_parent",
    )
    rows = _events(fake)
    assert len(rows) == 1
    assert rows[0]["type"] == PROPOSAL_ACCEPTED
    assert rows[0]["source"] == "mahalath"
    assert rows[0]["trace_id"] == "prop-1"
    assert rows[0]["session_id"] == "mahalath"
    assert rows[0]["metadata"]["action_type"] == "propose_parent"


def test_witness_never_raises_on_broken_db() -> None:
    class ExplodingDb:
        def __getitem__(self, name):
            raise RuntimeError("no db for you")

    witness = Witness(_enabled_config(), db=ExplodingDb())
    witness.emit(DEBATE_COMPLETED, trace_id="t1", summary="resilient")  # must not raise


def test_set_witness_overrides_singleton() -> None:
    fake = FakeDb()
    set_witness(Witness(_enabled_config(), db=fake))
    get_witness().emit(DEBATE_COMPLETED, trace_id="t2", summary="via singleton")
    assert len(_events(fake)) == 1


def test_accept_proposal_emits_event(mongo_db) -> None:
    """The proposals module testifies operator decisions through the witness."""
    from mahalath.actions import ProposeAlias, dispatch
    from mahalath.db.models import OntologyEntry
    from mahalath.db.repositories import OntologyEntryRepository
    from mahalath.proposals import accept_proposal

    fake = FakeDb()
    set_witness(Witness(_enabled_config(), db=fake))

    OntologyEntryRepository(mongo_db).insert(
        OntologyEntry(mpl_label="MPL-001", canonical_term="alpha", confidence=9.0)
    )
    result = dispatch(
        ProposeAlias(label="MPL-001", alias="first", reason="test", confidence=3.0),
        mongo_db,
    )
    assert result.status == "pending_review"

    accept_proposal(result.proposal_id, mongo_db, note="via test")
    types = [row["type"] for row in _events(fake)]
    assert PROPOSAL_ACCEPTED in types
