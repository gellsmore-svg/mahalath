"""Related-document linking and term correspondence (ADR-036).

The defining property is that this is NOT deduplication: the incoming
document is processed in full and the original's terms are untouched.
"""

from __future__ import annotations

import pytest

from mahalath.adapters.base import Adapter
from mahalath.db.models import DefinitionVersion, OntologyEntry
from mahalath.db.repositories import OntologyEntryRepository
from mahalath.relatedness import (
    BY_MODEL,
    RelatednessError,
    compare_linked_documents,
    correspond_terms,
    find_related_documents,
    parse_relatedness_verdict,
)


class Judge(Adapter):
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0

    def generate(self, prompt, model=None, want_json=False):
        self.calls += 1

        class R:
            text = self.reply

        return R()


def _doc(db, document_id, *, checksum, title, text, tmp_path):
    path = tmp_path / f"{document_id}.md"
    path.write_text(text, encoding="utf-8")
    db.documents.insert_one({
        "document_id": document_id, "checksum_sha256": checksum,
        "title": title, "archive_path": str(path),
    })


def _entry(db, label, term, doc_id, definition):
    OntologyEntryRepository(db).insert(OntologyEntry(
        mpl_label=label, canonical_term=term, confidence=8.0,
        definitions=[DefinitionVersion(text=definition)],
        source_document_ids=[doc_id],
    ))


# --- the judgement --------------------------------------------------------


def test_parse_rejects_an_unknown_relation() -> None:
    with pytest.raises(RelatednessError, match="unknown relation"):
        parse_relatedness_verdict('{"related": true, "relation": "vibes", "confidence": 9}')


def test_parse_rejects_out_of_range_confidence() -> None:
    with pytest.raises(RelatednessError, match="outside 0-10"):
        parse_relatedness_verdict('{"related": true, "relation": "revision", "confidence": 44}')


def test_parse_accepts_a_fenced_reply() -> None:
    verdict = parse_relatedness_verdict(
        '```json\n{"related": true, "relation": "revision", '
        '"confidence": 8.5, "rationale": "second edition"}\n```'
    )
    assert verdict["relation"] == "revision" and verdict["confidence"] == 8.5


def test_not_related_clears_the_relation() -> None:
    verdict = parse_relatedness_verdict('{"related": false, "relation": "revision", "confidence": 9}')
    assert verdict["related"] is False and verdict["relation"] is None


# --- linking --------------------------------------------------------------


def test_byte_identical_documents_cannot_both_exist(mongo_db, tmp_path) -> None:
    """So relatedness never has to special-case them (ADR-016 + unique index)."""
    from pymongo.errors import DuplicateKeyError

    _doc(mongo_db, "d1", checksum="same", title="A", text="x", tmp_path=tmp_path)
    with pytest.raises(DuplicateKeyError):
        _doc(mongo_db, "d2", checksum="same", title="A copy", text="x", tmp_path=tmp_path)


def test_model_judged_link_is_recorded_with_its_reasoning(mongo_db, tmp_path) -> None:
    _doc(mongo_db, "d1", checksum="c1", title="Bachelor's", text="The widget chapter.", tmp_path=tmp_path)
    _doc(mongo_db, "d2", checksum="c2", title="Bachelor's v2", text="The widget chapter, revised.", tmp_path=tmp_path)
    judge = Judge('{"related": true, "relation": "revision", "confidence": 9.0, "rationale": "same work, later edition"}')
    [link] = find_related_documents(mongo_db, "d2", judge)
    assert link.relation == "revision"
    assert link.established_by == BY_MODEL
    assert "later edition" in link.rationale


def test_low_confidence_is_not_recorded(mongo_db, tmp_path) -> None:
    _doc(mongo_db, "d1", checksum="c1", title="A", text="a", tmp_path=tmp_path)
    _doc(mongo_db, "d2", checksum="c2", title="B", text="b", tmp_path=tmp_path)
    judge = Judge('{"related": true, "relation": "shares_material", "confidence": 3.0}')
    assert find_related_documents(mongo_db, "d2", judge, min_confidence=6.0) == []


def test_an_unusable_reply_skips_rather_than_fails(mongo_db, tmp_path) -> None:
    _doc(mongo_db, "d1", checksum="c1", title="A", text="a", tmp_path=tmp_path)
    _doc(mongo_db, "d2", checksum="c2", title="B", text="b", tmp_path=tmp_path)
    assert find_related_documents(mongo_db, "d2", Judge("not json at all")) == []


def test_links_are_not_duplicated_on_a_second_run(mongo_db, tmp_path) -> None:
    _doc(mongo_db, "d1", checksum="c1", title="A", text="a", tmp_path=tmp_path)
    _doc(mongo_db, "d2", checksum="c2", title="B", text="b", tmp_path=tmp_path)
    reply = '{"related": true, "relation": "revision", "confidence": 9.0}'
    find_related_documents(mongo_db, "d2", Judge(reply))
    find_related_documents(mongo_db, "d2", Judge(reply))
    assert mongo_db.document_links.count_documents({}) == 1


# --- correspondence and comparison ---------------------------------------


def _linked_pair(mongo_db, tmp_path):
    _doc(mongo_db, "d1", checksum="c1", title="v1", text="a", tmp_path=tmp_path)
    _doc(mongo_db, "d2", checksum="c2", title="v2", text="b", tmp_path=tmp_path)
    judge = Judge('{"related": true, "relation": "revision", "confidence": 9.0}')
    [link] = find_related_documents(mongo_db, "d2", judge)
    return link


def test_correspondence_links_terms_without_touching_them(mongo_db, tmp_path) -> None:
    """The originals must survive the comparison entirely unmodified."""
    link = _linked_pair(mongo_db, tmp_path)
    _entry(mongo_db, "MPL-001", "widget", "d1", "A widget is a unit of work.")
    _entry(mongo_db, "MPL-002", "widget", "d2", "A widget is the smallest schedulable unit.")
    before = mongo_db.ontology_entries.find_one({"_id": "MPL-001"})

    [correspondence] = correspond_terms(mongo_db, link.link_id)
    assert {correspondence.mpl_label, correspondence.related_mpl_label} == {
        "MPL-001", "MPL-002"
    }
    after = mongo_db.ontology_entries.find_one({"_id": "MPL-001"})
    assert after == before, "the original entry must be untouched"


def test_comparison_reports_what_actually_differs(mongo_db, tmp_path) -> None:
    link = _linked_pair(mongo_db, tmp_path)
    _entry(mongo_db, "MPL-001", "widget", "d1", "A widget is a unit of work.")
    _entry(mongo_db, "MPL-002", "widget", "d2", "A widget is the smallest schedulable unit.")
    _entry(mongo_db, "MPL-003", "sprocket", "d1", "Only in the first run.")
    _entry(mongo_db, "MPL-004", "gubbin", "d2", "Only in the second run.")

    report = compare_linked_documents(mongo_db, link.link_id)
    assert report["shared_terms"] == 1
    assert report["only_in_document"] == ["gubbin"] or report["only_in_related"] == ["gubbin"]
    [differing] = report["differing_definitions"]
    assert differing["term"] == "widget"
    # The link is oriented from the document that was judged (d2) to the one it
    # was judged against (d1); the report names both so orientation is readable.
    assert {differing["definition"], differing["related_definition"]} == {
        "A widget is a unit of work.",
        "A widget is the smallest schedulable unit.",
    }
    assert report["document_title"] and report["related_document_title"]


def test_identical_definitions_are_not_reported_as_differing(mongo_db, tmp_path) -> None:
    link = _linked_pair(mongo_db, tmp_path)
    _entry(mongo_db, "MPL-001", "widget", "d1", "A widget is a unit of work.")
    _entry(mongo_db, "MPL-002", "widget", "d2", "A widget is a unit of work.")
    report = compare_linked_documents(mongo_db, link.link_id)
    assert report["shared_terms"] == 1
    assert report["differing_definitions"] == []


def test_comparing_an_unknown_link_refuses(mongo_db) -> None:
    with pytest.raises(RelatednessError, match="no link"):
        compare_linked_documents(mongo_db, "nope")


# --- ADR-033 prerequisite: which document triggered a write ---------------


def test_redebate_records_the_triggering_document_not_the_first(mongo_db) -> None:
    """33 of the live corpus's entries have several sources; the first is a guess."""
    from mahalath.adapters import MockAdapter
    from mahalath.config import RuntimeConfig
    from mahalath.ontology import redebate_entry

    OntologyEntryRepository(mongo_db).insert(OntologyEntry(
        mpl_label="MPL-800", canonical_term="widget", confidence=8.0,
        definitions=[DefinitionVersion(text="A widget.")],
        source_document_ids=["doc-first", "doc-triggering"],
    ))
    import json as _json

    adapter = MockAdapter(default_response=_json.dumps({
        "definition": "A widget is a unit of scheduled work.",
        "rationale": "ok", "confidence": 9.0,
    }))
    result = redebate_entry(
        mongo_db, "MPL-800", "some context", adapter,
        RuntimeConfig(max_iterations_per_term=3, confidence_threshold=8.0),
        triggering_document_id="doc-triggering",
        apply=True,
    )
    assert result.outcome == "accepted"
    logged = mongo_db.decision_log.find_one({"term": "widget"})
    assert logged["source_document_id"] == "doc-triggering", (
        "the audit must name the document that triggered the write, "
        "not source_document_ids[0]"
    )
