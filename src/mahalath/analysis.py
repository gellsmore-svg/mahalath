"""Decision-effectiveness self-analysis (requirements §3.4).

Everything Mahalath decides is already audited: debates land in
`decision_log`, hierarchy reviews in `ontology_reviews`, agent
proposals — with the operator's accept/reject/rollback verdicts — in
`action_proposals`, inconclusive terms in `undecided_queue`. This
module reads those trails back and asks the §3.4 question: *is the
decision-making actually working?*

The headline signal is **calibration**: operator decisions on agent
proposals are a labelled dataset for agent confidence. If operator
acceptance rates rise with the agent's stated confidence, confidence
means something; if they don't, the threshold knobs are tuning noise.

Strictly read-only over the existing nine collections — analysing
decisions must never become another writer of them. The periodic
snapshot (scheduler REM tail + `mahalath effectiveness --snapshot`)
is appended to `logs/effectiveness.jsonl`, file-based like the
process logs, so trend data accrues without a schema change.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from pymongo.database import Database

from mahalath.config import AppConfig

log = logging.getLogger("mahalath.analysis")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- Report sections --------------------------------------------------------


@dataclass
class DebateStats:
    """Outcomes of the extraction debates in `decision_log`."""

    total: int = 0
    by_outcome: dict[str, int] = field(default_factory=dict)
    acceptance_rate: float | None = None
    avg_iterations_by_outcome: dict[str, float] = field(default_factory=dict)
    avg_confidence_by_outcome: dict[str, float] = field(default_factory=dict)


@dataclass
class CalibrationBand:
    """Operator verdicts on proposals within one agent-confidence band."""

    lo: float
    hi: float
    decided: int = 0
    accepted: int = 0
    rejected: int = 0
    rolled_back: int = 0

    @property
    def acceptance_rate(self) -> float | None:
        return self.accepted / self.decided if self.decided else None


@dataclass
class ProposalStats:
    """Agent action proposals and the operator decisions on them."""

    total: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    by_action_type: dict[str, int] = field(default_factory=dict)
    operator_decided: int = 0
    operator_accepted: int = 0
    operator_rejected: int = 0
    operator_rolled_back: int = 0
    # Verdicts rendered by an LLM on delegated authority
    # (decided_via="claude_delegate"). Counted separately and EXCLUDED
    # from the calibration bands: the metric asks whether agent
    # confidence predicts *human* agreement, and a delegate's verdicts
    # would let the model grade its own homework.
    delegated_decided: int = 0
    avg_confidence_accepted: float | None = None
    avg_confidence_rejected: float | None = None
    bands: list[CalibrationBand] = field(default_factory=list)


@dataclass
class QueueStats:
    """Undecided-queue health."""

    size: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)
    by_escalation: dict[str, int] = field(default_factory=dict)
    at_max_escalation: int = 0
    oldest_age_days: float | None = None
    avg_last_confidence: float | None = None


@dataclass
class RedebateStats:
    """Did re-debating (REM) eventually resolve undecided terms?

    A term is `resolved` when an accepted debate follows an undecided
    one in its decision_log history. Terms whose history contains an
    undecided outcome are the denominator.
    """

    terms_with_undecided: int = 0
    terms_resolved: int = 0

    @property
    def resolution_rate(self) -> float | None:
        if not self.terms_with_undecided:
            return None
        return self.terms_resolved / self.terms_with_undecided


@dataclass
class ReviewStats:
    """Hierarchy-review yield from `ontology_reviews`."""

    total: int = 0
    with_actions: int = 0
    by_trigger: dict[str, int] = field(default_factory=dict)
    avg_actions: float | None = None

    @property
    def no_action_rate(self) -> float | None:
        if not self.total:
            return None
        return (self.total - self.with_actions) / self.total


@dataclass
class CoverageStats:
    """Current ontology state the decisions have produced."""

    entries: int = 0
    stale_entries: int = 0
    avg_entry_confidence: float | None = None
    definitions: int = 0
    frame_tagged: int = 0
    intent_annotated: int = 0


@dataclass
class EffectivenessReport:
    generated_at: datetime
    database: str
    debates: DebateStats
    proposals: ProposalStats
    queue: QueueStats
    redebates: RedebateStats
    reviews: ReviewStats
    coverage: CoverageStats
    findings: list[str] = field(default_factory=list)


# Operator-decision verdicts counted as "operator engaged with this".
_DECIDED = ("accepted", "rejected", "rolled_back")

# Confidence bands on the standard 0–10 scale.
_BAND_EDGES = [(0.0, 2.5), (2.5, 5.0), (5.0, 7.5), (7.5, 10.0)]

# Mirrors rem.rem_review's default max_escalation: items at or past it
# are no longer re-debated and sit waiting for the operator.
_MAX_ESCALATION = 3


# --- Section builders -------------------------------------------------------


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def _debate_stats(db: Database) -> DebateStats:
    stats = DebateStats()
    for row in db.decision_log.aggregate([
        {"$group": {
            "_id": "$outcome",
            "n": {"$sum": 1},
            "avg_iterations": {"$avg": "$iterations_used"},
            "avg_confidence": {"$avg": "$final_confidence"},
        }},
    ]):
        outcome = row["_id"] or "unknown"
        stats.by_outcome[outcome] = row["n"]
        stats.total += row["n"]
        if row["avg_iterations"] is not None:
            stats.avg_iterations_by_outcome[outcome] = round(
                row["avg_iterations"], 2
            )
        if row["avg_confidence"] is not None:
            stats.avg_confidence_by_outcome[outcome] = round(
                row["avg_confidence"], 2
            )
    if stats.total:
        stats.acceptance_rate = round(
            stats.by_outcome.get("accepted", 0) / stats.total, 3
        )
    return stats


def _proposal_stats(db: Database) -> ProposalStats:
    stats = ProposalStats()
    for row in db.action_proposals.aggregate([
        {"$group": {"_id": "$status", "n": {"$sum": 1}}},
    ]):
        stats.by_status[row["_id"] or "unknown"] = row["n"]
        stats.total += row["n"]
    for row in db.action_proposals.aggregate([
        {"$group": {"_id": "$action_type", "n": {"$sum": 1}}},
    ]):
        stats.by_action_type[row["_id"] or "unknown"] = row["n"]

    # The calibration dataset: every proposal the operator ruled on,
    # joined back to the confidence the agent stated when proposing it.
    # Delegated verdicts (an LLM acting for the operator) are counted
    # but excluded — legacy rows without decided_via read as operator.
    all_decided = list(db.action_proposals.find(
        {"operator_decision": {"$in": list(_DECIDED)}},
        {"confidence": 1, "operator_decision": 1, "decided_via": 1},
    ))
    decided = [
        d for d in all_decided
        if d.get("decided_via") != "claude_delegate"
    ]
    stats.delegated_decided = len(all_decided) - len(decided)
    stats.operator_decided = len(decided)

    bands = [CalibrationBand(lo=lo, hi=hi) for lo, hi in _BAND_EDGES]
    accepted_confs: list[float] = []
    rejected_confs: list[float] = []
    for doc in decided:
        verdict = doc["operator_decision"]
        conf = float(doc.get("confidence") or 0.0)
        if verdict == "accepted":
            stats.operator_accepted += 1
            accepted_confs.append(conf)
        elif verdict == "rejected":
            stats.operator_rejected += 1
            rejected_confs.append(conf)
        else:
            stats.operator_rolled_back += 1
        for band in bands:
            # Last band is closed above so conf=10.0 lands somewhere.
            in_band = (
                band.lo <= conf < band.hi
                or (band.hi == _BAND_EDGES[-1][1] and conf >= band.hi)
            )
            if in_band:
                band.decided += 1
                if verdict == "accepted":
                    band.accepted += 1
                elif verdict == "rejected":
                    band.rejected += 1
                else:
                    band.rolled_back += 1
                break
    stats.bands = bands
    if accepted_confs:
        stats.avg_confidence_accepted = round(
            sum(accepted_confs) / len(accepted_confs), 2
        )
    if rejected_confs:
        stats.avg_confidence_rejected = round(
            sum(rejected_confs) / len(rejected_confs), 2
        )
    return stats


def _queue_stats(db: Database) -> QueueStats:
    stats = QueueStats(size=db.undecided_queue.count_documents({}))
    for row in db.undecided_queue.aggregate([
        {"$group": {"_id": "$reason", "n": {"$sum": 1}}},
    ]):
        stats.by_reason[row["_id"] or "unknown"] = row["n"]
    for row in db.undecided_queue.aggregate([
        {"$group": {"_id": "$escalation_level", "n": {"$sum": 1}}},
    ]):
        level = row["_id"] if row["_id"] is not None else 0
        stats.by_escalation[str(level)] = row["n"]
        if level >= _MAX_ESCALATION:
            stats.at_max_escalation += row["n"]
    summary = list(db.undecided_queue.aggregate([
        {"$group": {
            "_id": None,
            "oldest": {"$min": "$created_at"},
            "avg_conf": {"$avg": "$last_confidence"},
        }},
    ]))
    if summary:
        oldest = summary[0]["oldest"]
        if isinstance(oldest, datetime):
            if oldest.tzinfo is None:  # Mongo round-trips naive UTC
                oldest = oldest.replace(tzinfo=timezone.utc)
            stats.oldest_age_days = round(
                (_utcnow() - oldest).total_seconds() / 86400, 1
            )
        stats.avg_last_confidence = _round(summary[0]["avg_conf"])
    return stats


def _redebate_stats(db: Database) -> RedebateStats:
    """Walk per-term debate histories for undecided→accepted arcs.

    Grouped server-side; the per-term outcome scan happens in Python
    because "an accepted AFTER the first undecided" doesn't reduce to
    one aggregation operator. Distinct debated terms are bounded by
    corpus vocabulary, so the cursor stays small.
    """
    stats = RedebateStats()
    for row in db.decision_log.aggregate([
        {"$sort": {"term": 1, "created_at": 1}},
        {"$group": {"_id": "$term", "outcomes": {"$push": "$outcome"}}},
    ]):
        outcomes = row["outcomes"]
        if "undecided" not in outcomes:
            continue
        stats.terms_with_undecided += 1
        first_undecided = outcomes.index("undecided")
        if "accepted" in outcomes[first_undecided + 1:]:
            stats.terms_resolved += 1
    return stats


def _review_stats(db: Database) -> ReviewStats:
    stats = ReviewStats()
    summary = list(db.ontology_reviews.aggregate([
        {"$group": {
            "_id": None,
            "total": {"$sum": 1},
            "with_actions": {
                "$sum": {"$cond": [{"$gt": ["$actions_count", 0]}, 1, 0]}
            },
            "avg_actions": {"$avg": "$actions_count"},
        }},
    ]))
    if summary:
        stats.total = summary[0]["total"]
        stats.with_actions = summary[0]["with_actions"]
        stats.avg_actions = _round(summary[0]["avg_actions"])
    for row in db.ontology_reviews.aggregate([
        {"$group": {"_id": "$triggered_by", "n": {"$sum": 1}}},
    ]):
        stats.by_trigger[row["_id"] or "unknown"] = row["n"]
    return stats


def _coverage_stats(db: Database) -> CoverageStats:
    stats = CoverageStats(
        entries=db.ontology_entries.count_documents({}),
        stale_entries=db.ontology_entries.count_documents({"is_stale": True}),
    )
    conf = list(db.ontology_entries.aggregate([
        {"$group": {"_id": None, "avg": {"$avg": "$confidence"}}},
    ]))
    if conf:
        stats.avg_entry_confidence = _round(conf[0]["avg"])
    defs = list(db.ontology_entries.aggregate([
        {"$project": {"definitions": 1}},
        {"$unwind": "$definitions"},
        {"$group": {
            "_id": None,
            "total": {"$sum": 1},
            "framed": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$definitions.context_id", None]}, None]},
                1, 0,
            ]}},
            "intent": {"$sum": {"$cond": [
                {"$gt": [
                    {"$size": {"$ifNull": ["$definitions.intent_tags", []]}},
                    0,
                ]},
                1, 0,
            ]}},
        }},
    ]))
    if defs:
        stats.definitions = defs[0]["total"]
        stats.frame_tagged = defs[0]["framed"]
        stats.intent_annotated = defs[0]["intent"]
    return stats


# --- Findings (the self-analysis) -------------------------------------------

_MIN_BAND_DECISIONS = 3  # bands thinner than this can't anchor a verdict


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def _calibration_findings(proposals: ProposalStats) -> list[str]:
    populated = [
        b for b in proposals.bands if b.decided >= _MIN_BAND_DECISIONS
    ]
    if len(populated) < 2:
        return [
            "Calibration: not enough operator decisions to assess whether "
            f"agent confidence predicts operator agreement "
            f"({proposals.operator_decided} decided proposals so far; need "
            f"at least {_MIN_BAND_DECISIONS} in each of two confidence "
            "bands)."
        ]
    low, high = populated[0], populated[-1]
    low_rate = low.acceptance_rate or 0.0
    high_rate = high.acceptance_rate or 0.0
    detail = (
        f"operator acceptance {_pct(low_rate)} in the "
        f"{low.lo:g}–{low.hi:g} confidence band vs {_pct(high_rate)} in "
        f"{high.lo:g}–{high.hi:g}"
    )
    if high_rate > low_rate:
        return [f"Calibration: agent confidence is predictive — {detail}."]
    return [
        f"Calibration: agent confidence is NOT predicting operator "
        f"agreement — {detail}. Confidence-threshold tuning is currently "
        "tuning noise."
    ]


def _build_findings(report: EffectivenessReport) -> list[str]:
    findings = _calibration_findings(report.proposals)

    d = report.debates
    if d.total and d.acceptance_rate is not None:
        undecided = d.by_outcome.get("undecided", 0)
        line = (
            f"Debates: {_pct(d.acceptance_rate)} of {d.total} debates "
            "ended accepted."
        )
        if undecided / d.total > 0.5:
            line += (
                f" Over half ({undecided}) ended undecided — the "
                "confidence threshold may be too strict for the current "
                "model."
            )
        findings.append(line)

    r = report.redebates
    if r.terms_with_undecided:
        findings.append(
            f"Re-debate: {r.terms_resolved} of {r.terms_with_undecided} "
            f"once-undecided terms ({_pct(r.resolution_rate or 0.0)}) were "
            "later accepted."
        )

    q = report.queue
    if q.at_max_escalation:
        findings.append(
            f"Queue: {q.at_max_escalation} undecided item(s) have reached "
            f"max escalation ({_MAX_ESCALATION}) — REM will not retry them; "
            "they need an operator decision."
        )

    v = report.reviews
    if v.total and v.no_action_rate is not None and v.no_action_rate > 0.9:
        findings.append(
            f"Hierarchy reviews: {_pct(v.no_action_rate)} of {v.total} "
            "review passes proposed no action — review spend is mostly "
            "confirming the status quo."
        )

    c = report.coverage
    if c.stale_entries:
        findings.append(
            f"Staleness: {c.stale_entries} of {c.entries} entries currently "
            "stale, awaiting the self-healing pass."
        )

    if not findings:
        findings.append("No data yet — process a document first.")
    return findings


# --- Public API -------------------------------------------------------------


def build_effectiveness_report(
    db: Database, *, database_name: str
) -> EffectivenessReport:
    """Compute the §3.4 self-analysis over one database. Read-only."""
    report = EffectivenessReport(
        generated_at=_utcnow(),
        database=database_name,
        debates=_debate_stats(db),
        proposals=_proposal_stats(db),
        queue=_queue_stats(db),
        redebates=_redebate_stats(db),
        reviews=_review_stats(db),
        coverage=_coverage_stats(db),
    )
    report.findings = _build_findings(report)
    return report


def report_to_dict(report: EffectivenessReport) -> dict:
    """JSON-safe dict, with the derived rates materialised."""
    data = asdict(report)
    data["generated_at"] = report.generated_at.isoformat(timespec="seconds")
    data["redebates"]["resolution_rate"] = _round(
        report.redebates.resolution_rate, 3
    )
    data["reviews"]["no_action_rate"] = _round(
        report.reviews.no_action_rate, 3
    )
    for band, band_data in zip(report.proposals.bands, data["proposals"]["bands"]):
        band_data["acceptance_rate"] = _round(band.acceptance_rate, 3)
    return data


def render_report_lines(report: EffectivenessReport) -> list[str]:
    """Plain-text rendering shared by the CLI and the snapshot log."""
    d, p, q, r, v, c = (
        report.debates, report.proposals, report.queue,
        report.redebates, report.reviews, report.coverage,
    )

    def counts(mapping: dict[str, int]) -> str:
        if not mapping:
            return "(none)"
        return ", ".join(f"{k}={n}" for k, n in sorted(mapping.items()))

    lines = [
        f"Decision-effectiveness report — {report.database} "
        f"@ {report.generated_at.isoformat(timespec='seconds')}",
        "",
        "Findings:",
        *[f"  - {f}" for f in report.findings],
        "",
        f"Debates: {d.total} total ({counts(d.by_outcome)})",
    ]
    if d.acceptance_rate is not None:
        lines.append(f"  acceptance rate: {_pct(d.acceptance_rate)}")
    for outcome in sorted(d.avg_iterations_by_outcome):
        conf = d.avg_confidence_by_outcome.get(outcome)
        conf_part = f", avg confidence {conf}" if conf is not None else ""
        lines.append(
            f"  {outcome}: avg iterations "
            f"{d.avg_iterations_by_outcome[outcome]}{conf_part}"
        )

    lines += [
        "",
        f"Proposals: {p.total} total ({counts(p.by_status)})",
        f"  by action type: {counts(p.by_action_type)}",
        f"  operator decided: {p.operator_decided} "
        f"(accepted={p.operator_accepted}, rejected={p.operator_rejected}, "
        f"rolled_back={p.operator_rolled_back})"
        + (f" — plus {p.delegated_decided} delegated verdict(s), "
           "excluded from calibration"
           if p.delegated_decided else ""),
    ]
    if p.avg_confidence_accepted is not None or p.avg_confidence_rejected is not None:
        lines.append(
            f"  avg agent confidence when operator accepted: "
            f"{p.avg_confidence_accepted} / when rejected: "
            f"{p.avg_confidence_rejected}"
        )
    for band in p.bands:
        if not band.decided:
            continue
        rate = band.acceptance_rate or 0.0
        lines.append(
            f"  confidence {band.lo:g}–{band.hi:g}: {band.decided} decided, "
            f"{_pct(rate)} accepted"
        )

    lines += [
        "",
        f"Undecided queue: {q.size} item(s) ({counts(q.by_reason)})",
        f"  escalation levels: {counts(q.by_escalation)}"
        f" — {q.at_max_escalation} at max",
    ]
    if q.oldest_age_days is not None:
        lines.append(
            f"  oldest item: {q.oldest_age_days} days; "
            f"avg last confidence: {q.avg_last_confidence}"
        )

    if r.terms_with_undecided:
        lines += [
            "",
            f"Re-debate: {r.terms_resolved}/{r.terms_with_undecided} "
            f"once-undecided terms resolved "
            f"({_pct(r.resolution_rate or 0.0)})",
        ]

    lines += [
        "",
        f"Hierarchy reviews: {v.total} total ({counts(v.by_trigger)})",
    ]
    if v.total:
        lines.append(
            f"  with actions: {v.with_actions} "
            f"(no-action rate {_pct(v.no_action_rate or 0.0)}; "
            f"avg actions {v.avg_actions})"
        )

    lines += [
        "",
        f"Ontology: {c.entries} entries (avg confidence "
        f"{c.avg_entry_confidence}), {c.stale_entries} stale",
        f"  definitions: {c.definitions} "
        f"(frame-tagged {c.frame_tagged}, intent-annotated "
        f"{c.intent_annotated})",
    ]
    return lines


SNAPSHOT_FILENAME = "effectiveness.jsonl"


def write_effectiveness_snapshot(
    config: AppConfig, report: EffectivenessReport
) -> Path:
    """Append one JSON line to `logs/effectiveness.jsonl`.

    File-based like the per-document process logs: periodic snapshots
    accrue trend data without granting the analysis layer a Mongo
    write path.
    """
    logs_dir = Path.cwd() / config.paths.logs
    logs_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = logs_dir / SNAPSHOT_FILENAME
    with snapshot_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(report_to_dict(report), default=str) + "\n")
    return snapshot_path
