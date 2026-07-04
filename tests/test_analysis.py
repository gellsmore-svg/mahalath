"""Decision-effectiveness self-analysis tests (analysis.py, §3.4).

Covers: empty-DB report; debate outcome stats; re-debate resolution
arcs; operator-vs-confidence calibration (predictive, non-predictive,
and not-enough-data findings); queue health incl. max-escalation
finding; hierarchy-review yield; definition coverage; JSON dict
round-trip; text rendering; snapshot append.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from mahalath.analysis import (
    build_effectiveness_report,
    render_report_lines,
    report_to_dict,
    write_effectiveness_snapshot,
)
from mahalath.config import AppConfig, MongoConfig
from mahalath.db.models import (
    ActionProposal,
    DecisionLogEntry,
    DefinitionVersion,
    OntologyEntry,
    OntologyReview,
    UndecidedItem,
)
from mahalath.db.repositories import (
    ActionProposalRepository,
    DecisionLogRepository,
    OntologyEntryRepository,
    OntologyReviewRepository,
    UndecidedQueueRepository,
)

from conftest import TEST_DB_NAME


def _ts(minute: int) -> datetime:
    return datetime(2026, 6, 1, 12, minute, 0, tzinfo=timezone.utc)


def _decision(term: str, outcome: str, *, minute: int = 0,
              iterations: int = 3, confidence: float | None = 8.0) -> DecisionLogEntry:
    return DecisionLogEntry(
        term=term,
        source_document_id="doc-1",
        outcome=outcome,
        iterations_used=iterations,
        final_confidence=confidence,
        created_at=_ts(minute),
    )


def _proposal(confidence: float, operator_decision: str | None,
              action_type: str = "propose_parent") -> ActionProposal:
    return ActionProposal(
        action_type=action_type,
        confidence=confidence,
        status="pending_review",
        operator_decision=operator_decision,
    )


def _report(mongo_db):
    return build_effectiveness_report(mongo_db, database_name=TEST_DB_NAME)


# --- Empty database ---------------------------------------------------------


def test_empty_db_report_is_calm(mongo_db) -> None:
    report = _report(mongo_db)
    assert report.debates.total == 0
    assert report.debates.acceptance_rate is None
    assert report.proposals.operator_decided == 0
    assert report.queue.size == 0
    assert report.redebates.terms_with_undecided == 0
    assert report.coverage.entries == 0
    # The one finding states the calibration dataset is too small.
    assert any("not enough operator decisions" in f for f in report.findings)


# --- Debates ----------------------------------------------------------------


def test_debate_outcome_stats(mongo_db) -> None:
    repo = DecisionLogRepository(mongo_db)
    repo.insert(_decision("alpha", "accepted", minute=1, iterations=2))
    repo.insert(_decision("beta", "accepted", minute=2, iterations=4))
    repo.insert(_decision("gamma", "accepted", minute=3, iterations=6))
    repo.insert(_decision("delta", "undecided", minute=4, iterations=10,
                          confidence=5.0))

    d = _report(mongo_db).debates
    assert d.total == 4
    assert d.by_outcome == {"accepted": 3, "undecided": 1}
    assert d.acceptance_rate == 0.75
    assert d.avg_iterations_by_outcome["accepted"] == 4.0
    assert d.avg_confidence_by_outcome["undecided"] == 5.0


def test_majority_undecided_flagged_in_findings(mongo_db) -> None:
    repo = DecisionLogRepository(mongo_db)
    repo.insert(_decision("a", "accepted", minute=1))
    repo.insert(_decision("b", "undecided", minute=2))
    repo.insert(_decision("c", "undecided", minute=3))

    report = _report(mongo_db)
    assert any("Over half" in f for f in report.findings)


# --- Re-debate resolution ---------------------------------------------------


def test_redebate_resolution_arc(mongo_db) -> None:
    repo = DecisionLogRepository(mongo_db)
    # alpha: undecided, then accepted on re-debate → resolved.
    repo.insert(_decision("alpha", "undecided", minute=1))
    repo.insert(_decision("alpha", "accepted", minute=2))
    # beta: still undecided → unresolved.
    repo.insert(_decision("beta", "undecided", minute=3))
    # gamma: accepted BEFORE its undecided run → NOT a resolution arc.
    repo.insert(_decision("gamma", "accepted", minute=4))
    repo.insert(_decision("gamma", "undecided", minute=5))
    # delta: accepted first try → not in the denominator.
    repo.insert(_decision("delta", "accepted", minute=6))

    r = _report(mongo_db).redebates
    assert r.terms_with_undecided == 3
    assert r.terms_resolved == 1
    assert r.resolution_rate == 1 / 3


# --- Calibration ------------------------------------------------------------


def test_calibration_predictive(mongo_db) -> None:
    repo = ActionProposalRepository(mongo_db)
    for conf in (8.0, 8.5, 9.0):
        repo.insert(_proposal(conf, "accepted"))
    for conf in (1.0, 1.5, 2.0):
        repo.insert(_proposal(conf, "rejected"))
    repo.insert(_proposal(9.5, None))  # no verdict → not in the dataset

    report = _report(mongo_db)
    p = report.proposals
    assert p.total == 7
    assert p.operator_decided == 6
    assert p.operator_accepted == 3
    assert p.operator_rejected == 3
    assert p.avg_confidence_accepted == 8.5
    assert p.avg_confidence_rejected == 1.5

    low_band = next(b for b in p.bands if b.lo == 0.0)
    high_band = next(b for b in p.bands if b.hi == 10.0)
    assert low_band.decided == 3 and low_band.acceptance_rate == 0.0
    assert high_band.decided == 3 and high_band.acceptance_rate == 1.0
    assert any("confidence is predictive" in f for f in report.findings)


def test_calibration_not_predictive(mongo_db) -> None:
    repo = ActionProposalRepository(mongo_db)
    for conf in (8.0, 8.5, 9.0):
        repo.insert(_proposal(conf, "rejected"))
    for conf in (1.0, 1.5, 2.0):
        repo.insert(_proposal(conf, "accepted"))

    report = _report(mongo_db)
    assert any("NOT predicting" in f for f in report.findings)


def test_calibration_band_edge_ten_is_counted(mongo_db) -> None:
    repo = ActionProposalRepository(mongo_db)
    repo.insert(_proposal(10.0, "accepted"))

    p = _report(mongo_db).proposals
    high_band = next(b for b in p.bands if b.hi == 10.0)
    assert high_band.decided == 1 and high_band.accepted == 1


# --- Undecided queue --------------------------------------------------------


def test_queue_stats_and_max_escalation_finding(mongo_db) -> None:
    repo = UndecidedQueueRepository(mongo_db)
    repo.insert(UndecidedItem(
        decision_log_id="dl-1", term="alpha", source_document_id="doc-1",
        reason="below_threshold", last_confidence=5.0, escalation_level=0,
        created_at=_ts(0),
    ))
    repo.insert(UndecidedItem(
        decision_log_id="dl-2", term="beta", source_document_id="doc-1",
        reason="below_threshold", last_confidence=6.0, escalation_level=3,
        created_at=_ts(1),
    ))
    repo.insert(UndecidedItem(
        decision_log_id="dl-3", term="gamma", source_document_id="doc-1",
        reason="proposed_term", escalation_level=1, created_at=_ts(2),
    ))

    report = _report(mongo_db)
    q = report.queue
    assert q.size == 3
    assert q.by_reason == {"below_threshold": 2, "proposed_term": 1}
    assert q.by_escalation == {"0": 1, "1": 1, "3": 1}
    assert q.at_max_escalation == 1
    assert q.oldest_age_days is not None and q.oldest_age_days > 0
    assert q.avg_last_confidence == 5.5
    assert any("max escalation" in f for f in report.findings)


# --- Hierarchy reviews ------------------------------------------------------


def test_review_yield(mongo_db) -> None:
    repo = OntologyReviewRepository(mongo_db)
    repo.insert(OntologyReview(
        triggered_by="post_accept", model="mock", prompt="p", response="r",
        duration_ms=10, actions_count=2,
    ))
    repo.insert(OntologyReview(
        triggered_by="rem_audit", model="mock", prompt="p", response="r",
        duration_ms=10, actions_count=0, no_actions_reason="consistent",
    ))

    v = _report(mongo_db).reviews
    assert v.total == 2
    assert v.with_actions == 1
    assert v.no_action_rate == 0.5
    assert v.avg_actions == 1.0
    assert v.by_trigger == {"post_accept": 1, "rem_audit": 1}


def test_high_no_action_rate_flagged(mongo_db) -> None:
    repo = OntologyReviewRepository(mongo_db)
    for _ in range(20):
        repo.insert(OntologyReview(
            triggered_by="post_accept", model="mock", prompt="p",
            response="r", duration_ms=10, actions_count=0,
        ))

    report = _report(mongo_db)
    assert any("proposed no action" in f for f in report.findings)


# --- Coverage ---------------------------------------------------------------


def test_coverage_counts_frames_and_intents(mongo_db) -> None:
    repo = OntologyEntryRepository(mongo_db)
    repo.insert(OntologyEntry(
        mpl_label="MPL-001", canonical_term="Substrate", confidence=8.0,
        definitions=[
            DefinitionVersion(text="Framed + intented.", context_id="ctx-1",
                              intent_tags=["int-1"]),
            DefinitionVersion(text="Bare."),
        ],
    ))
    repo.insert(OntologyEntry(
        mpl_label="MPL-002", canonical_term="Vorton", confidence=6.0,
        is_stale=True,
        definitions=[DefinitionVersion(text="Framed.", context_id="ctx-1")],
    ))

    report = _report(mongo_db)
    c = report.coverage
    assert c.entries == 2
    assert c.stale_entries == 1
    assert c.avg_entry_confidence == 7.0
    assert c.definitions == 3
    assert c.frame_tagged == 2
    assert c.intent_annotated == 1
    assert any("Staleness: 1 of 2" in f for f in report.findings)


# --- Serialisation + rendering + snapshot -----------------------------------


def test_report_to_dict_is_json_safe(mongo_db) -> None:
    DecisionLogRepository(mongo_db).insert(_decision("alpha", "accepted"))
    ActionProposalRepository(mongo_db).insert(_proposal(9.0, "accepted"))

    data = report_to_dict(_report(mongo_db))
    payload = json.dumps(data)  # must not raise
    assert TEST_DB_NAME in payload
    assert data["debates"]["acceptance_rate"] == 1.0
    high_band = data["proposals"]["bands"][-1]
    assert high_band["acceptance_rate"] == 1.0
    assert "findings" in data and data["findings"]


def test_render_report_lines_smoke(mongo_db) -> None:
    DecisionLogRepository(mongo_db).insert(_decision("alpha", "accepted"))
    text = "\n".join(render_report_lines(_report(mongo_db)))
    assert "Decision-effectiveness report" in text
    assert "Findings:" in text
    assert "Debates: 1 total" in text


def test_snapshot_appends_json_lines(mongo_db, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = AppConfig(mongo=MongoConfig(database=TEST_DB_NAME))

    report = _report(mongo_db)
    path = write_effectiveness_snapshot(config, report)
    write_effectiveness_snapshot(config, report)

    assert path == tmp_path / "logs" / "effectiveness.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        parsed = json.loads(line)
        assert parsed["database"] == TEST_DB_NAME


def test_delegated_verdicts_excluded_from_calibration(mongo_db) -> None:
    repo = ActionProposalRepository(mongo_db)
    for conf in (8.0, 8.5, 9.0):
        repo.insert(_proposal(conf, "accepted"))
    # A delegate's verdict must not enter the operator-calibration data.
    delegated = _proposal(9.5, "rejected")
    delegated.decided_via = "claude_delegate"
    repo.insert(delegated)

    p = _report(mongo_db).proposals
    assert p.operator_decided == 3
    assert p.delegated_decided == 1
    assert p.operator_rejected == 0  # the delegated rejection is not counted
    high_band = next(b for b in p.bands if b.hi == 10.0)
    assert high_band.decided == 3 and high_band.acceptance_rate == 1.0
