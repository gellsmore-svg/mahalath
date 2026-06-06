"""Mahalath CLI entry point.

Commands available:

    mahalath db-ping                                MongoDB connectivity check
    mahalath show-config                            print loaded config as JSON
    mahalath ingest-one <path>                      ingest a single Markdown
                                                    document
    mahalath process-document <document_id>         extract terms, debate, and
                                                    persist accepted entries
                                                    with hierarchy review
    mahalath list-ontology                          list ontology entries
    mahalath list-proposals [--status STATUS]       list action proposals
    mahalath show-proposal <proposal_id>            show full proposal record
    mahalath accept-proposal <proposal_id>          apply a pending_review
       [--note TEXT]                                proposal as operator
    mahalath reject-proposal <proposal_id>          mark a pending_review
       [--note TEXT]                                proposal rejected
    mahalath rollback-proposal <proposal_id>        undo an applied proposal
       [--note TEXT]

Stage 1 placeholders (raise an explicit "not yet implemented" error):

    mahalath debate-one <term>
    mahalath process-input

Exit codes (in addition to the conventional 0=success):

    2  unknown / not-yet-implemented subcommand requested
    3  pymongo not installed
    4  MongoDB connection failure
    5  source file not found / not readable (ingest-one)
    6  document_id not found in DB (process-document)
    7  archived source file missing on disk (process-document)
    9  proposal-workflow error (proposal not found, wrong status, etc.)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from mahalath.config import AppConfig, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mahalath")
    subcommands = parser.add_subparsers(dest="command")

    subcommands.add_parser("db-ping", help="Ping the configured MongoDB instance.")
    subcommands.add_parser("show-config", help="Print the loaded configuration as JSON.")

    ingest_one = subcommands.add_parser(
        "ingest-one", help="(Stage 1) Ingest a single document by path."
    )
    ingest_one.add_argument("path")

    debate_one = subcommands.add_parser(
        "debate-one", help="(Stage 1) Run one debate cycle on a single candidate term."
    )
    debate_one.add_argument("term")

    process_doc = subcommands.add_parser(
        "process-document",
        help="Extract candidate terms from an ingested document, run "
        "debate on the first N, and persist the results.",
    )
    process_doc.add_argument("document_id")
    process_doc.add_argument(
        "--max-terms",
        type=int,
        default=1,
        help="How many candidate terms to debate this run (default: 1).",
    )
    process_doc.add_argument(
        "--no-hierarchy-review",
        action="store_true",
        help="Skip the post-accept hierarchy-review pass.",
    )
    process_doc.add_argument(
        "--consensus-passes",
        type=int,
        default=None,
        help=(
            "Override the runtime config's hierarchy_consensus_passes "
            "(default 3) for this run. Pass 1 for single-pass (faster, "
            "no direction-variance protection)."
        ),
    )

    subcommands.add_parser(
        "process-input",
        help="(Stage 1) Process anything in the watched input/ folder.",
    )
    subcommands.add_parser(
        "list-ontology", help="List ontology entries."
    )

    list_proposals_parser = subcommands.add_parser(
        "list-proposals", help="List action proposals from action_proposals."
    )
    list_proposals_parser.add_argument(
        "--status",
        choices=["proposed", "applied", "pending_review", "invalid",
                 "rejected", "rolled_back"],
        default=None,
        help="Filter by status (default: show all).",
    )

    show_proposal_parser = subcommands.add_parser(
        "show-proposal",
        help="Print a single ActionProposal record as JSON.",
    )
    show_proposal_parser.add_argument("proposal_id")

    for cmd, help_text in [
        ("accept-proposal", "Accept a pending_review proposal (applies it)."),
        ("reject-proposal", "Reject a pending_review proposal."),
        ("rollback-proposal", "Undo an applied proposal."),
    ]:
        parser_ = subcommands.add_parser(cmd, help=help_text)
        parser_.add_argument("proposal_id")
        parser_.add_argument(
            "--note", default=None,
            help="Optional operator note recorded with the decision.",
        )

    export_glossary_parser = subcommands.add_parser(
        "export-glossary",
        help="Export the ontology as a Markdown or JSON glossary.",
    )
    export_glossary_parser.add_argument(
        "--format", choices=["md", "json"], default="md",
        help="Output format (default: md).",
    )
    export_glossary_parser.add_argument(
        "--out", default=None,
        help="Output file path. Defaults to stdout.",
    )

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    config = load_config()

    if args.command == "show-config":
        print(json.dumps(_config_as_dict(config), indent=2, default=str))
        return 0

    if args.command == "db-ping":
        return _db_ping(config)

    if args.command == "ingest-one":
        return _ingest_one(config, Path(args.path))

    if args.command == "process-document":
        return _process_document(
            config,
            args.document_id,
            max_terms=args.max_terms,
            skip_hierarchy_review=args.no_hierarchy_review,
            consensus_passes_override=args.consensus_passes,
        )

    if args.command == "list-ontology":
        return _list_ontology(config)

    if args.command == "list-proposals":
        return _list_proposals(config, status=args.status)

    if args.command == "show-proposal":
        return _show_proposal(config, args.proposal_id)

    if args.command == "accept-proposal":
        return _operator_decision(
            config, args.proposal_id, kind="accept", note=args.note
        )

    if args.command == "reject-proposal":
        return _operator_decision(
            config, args.proposal_id, kind="reject", note=args.note
        )

    if args.command == "rollback-proposal":
        return _operator_decision(
            config, args.proposal_id, kind="rollback", note=args.note
        )

    if args.command == "export-glossary":
        return _export_glossary(
            config, fmt=args.format,
            out_path=Path(args.out) if args.out else None,
        )

    if args.command in {"debate-one", "process-input"}:
        print(
            f"mahalath: command {args.command!r} is not yet implemented.",
            file=sys.stderr,
        )
        return 2

    parser.print_help()
    return 0


def _ingest_one(config: AppConfig, source_path: Path) -> int:
    from mahalath.db import close_all, ensure_indexes, get_database
    from mahalath.ingestion import IngestionError, ingest_one

    try:
        db = get_database(config)
        ensure_indexes(db)
    except Exception as exc:  # pragma: no cover - exercised by db-ping path
        print(f"mahalath: MongoDB unreachable: {exc}", file=sys.stderr)
        return 4

    try:
        result = ingest_one(source_path, config, db)
    except IngestionError as exc:
        print(f"mahalath: ingestion failed: {exc}", file=sys.stderr)
        return 5
    finally:
        close_all()

    payload = {
        "ok": True,
        "duplicate": result.duplicate,
        "document_id": result.document.document_id,
        "title": result.document.title,
        "checksum_sha256": result.document.checksum_sha256,
        "archive_path": result.document.archive_path,
        "byte_size": result.document.byte_size,
        "char_count": result.document.char_count,
        "activity_log_path": (
            str(result.activity_log_path) if result.activity_log_path else None
        ),
    }
    print(json.dumps(payload, indent=2))
    return 0


def _process_document(
    config: AppConfig,
    document_id: str,
    *,
    max_terms: int,
    skip_hierarchy_review: bool = False,
    consensus_passes_override: int | None = None,
) -> int:
    from mahalath.actions import dispatch
    from mahalath.adapters import make_adapter
    from mahalath.adapters.base import AdapterError
    from mahalath.chunking import extract_candidates_chunked
    from mahalath.db import close_all, ensure_indexes, get_database
    from mahalath.db.repositories import DocumentRepository
    from mahalath.debate import DebateError, run_debate
    from mahalath.extraction import ExtractionError
    from mahalath.hierarchy import (
        HierarchyReviewError,
        run_hierarchy_review_consensus,
    )
    from mahalath.ontology import persist_debate_result

    try:
        db = get_database(config)
        ensure_indexes(db)
    except Exception as exc:  # pragma: no cover
        print(f"mahalath: MongoDB unreachable: {exc}", file=sys.stderr)
        return 4

    try:
        doc_repo = DocumentRepository(db)
        document = doc_repo.find_by_document_id(document_id)
        if document is None:
            print(f"mahalath: document not found: {document_id}", file=sys.stderr)
            return 6

        archive_path = Path(document.archive_path)
        if not archive_path.is_absolute():
            archive_path = Path.cwd() / archive_path
        if not archive_path.exists():
            print(f"mahalath: archived source missing: {archive_path}", file=sys.stderr)
            return 7
        text = archive_path.read_text(encoding="utf-8", errors="replace")

        adapter = make_adapter(config.runtime.model_adapter, config)

        try:
            candidates = extract_candidates_chunked(text, adapter)
        except ExtractionError as exc:
            print(f"mahalath: extraction failed: {exc}", file=sys.stderr)
            return 8

        debated: list[dict[str, Any]] = []
        for candidate in candidates[:max_terms]:
            try:
                debate_result = run_debate(
                    term=candidate.term,
                    context=candidate.context,
                    source_document_id=document_id,
                    adapter=adapter,
                    runtime=config.runtime,
                )
            except (DebateError, AdapterError) as exc:
                debated.append({
                    "term": candidate.term,
                    "outcome": "error",
                    "error": str(exc),
                })
                continue

            persist_result = persist_debate_result(debate_result, db, config.runtime)
            term_record = {
                "term": candidate.term,
                "outcome": debate_result.outcome,
                "final_confidence": debate_result.final_confidence,
                "final_definition": debate_result.final_definition,
                "iterations_used": debate_result.iterations_used,
                "mpl_label": persist_result.mpl_label,
                "decision_log_id": debate_result.decision_log_id,
            }

            if (
                not skip_hierarchy_review
                and persist_result.outcome == "accepted"
                and persist_result.mpl_label is not None
            ):
                term_record["hierarchy_review"] = _run_and_dispatch_review(
                    db, adapter, config, persist_result.mpl_label,
                    source_decision_log_id=debate_result.decision_log_id,
                    dispatch_fn=dispatch,
                    review_fn=run_hierarchy_review_consensus,
                    review_exc=HierarchyReviewError,
                    consensus_passes_override=consensus_passes_override,
                )

            debated.append(term_record)

        doc_repo.mark_processed(document_id)

        log_path = _write_process_log(
            config, document_id, document.title, candidates, debated
        )

        payload = {
            "ok": True,
            "document_id": document_id,
            "title": document.title,
            "candidates_extracted": len(candidates),
            "debated": debated,
            "remaining_candidates": [c.term for c in candidates[max_terms:]],
            "activity_log_path": str(log_path),
        }
        print(json.dumps(payload, indent=2))
        return 0
    finally:
        close_all()


def _run_and_dispatch_review(
    db,
    adapter,
    config: AppConfig,
    focus_label: str,
    *,
    source_decision_log_id: str,
    dispatch_fn,
    review_fn,
    review_exc,
    consensus_passes_override: int | None = None,
) -> dict[str, Any]:
    try:
        review = review_fn(
            focus_label,
            db,
            adapter,
            config.runtime,
            n_passes=consensus_passes_override,
            triggered_by="post_accept",
            source_decision_log_id=source_decision_log_id,
        )
    except review_exc as exc:
        return {"error": str(exc)}

    action_records: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}
    for action in review.consensus_actions:
        dispatch_result = dispatch_fn(action, db)
        status_counts[dispatch_result.status] = (
            status_counts.get(dispatch_result.status, 0) + 1
        )
        action_records.append({
            "type": dispatch_result.action_type,
            "status": dispatch_result.status,
            "detail": dispatch_result.detail,
            "payload": dispatch_result.payload,
            "confidence": action.confidence,
            "reason": action.reason,
            "proposal_id": dispatch_result.proposal_id,
        })

    per_pass_summary = [
        [
            {"type": a.action_type, "payload": a.payload(), "conf": a.confidence}
            for a in pass_actions
        ]
        for pass_actions in review.per_pass_actions
    ]

    return {
        "review_ids": review.review_ids,
        "n_passes": review.n_passes,
        "total_duration_ms": review.total_duration_ms,
        "consensus_actions_count": len(review.consensus_actions),
        "per_pass_proposals": per_pass_summary,
        "status_counts": status_counts,
        "actions": action_records,
    }


def _write_process_log(
    config: AppConfig,
    document_id: str,
    title: str | None,
    candidates: list,
    debated: list[dict[str, Any]],
) -> Path:
    from datetime import datetime, timezone

    logs_dir = Path.cwd() / config.paths.logs
    logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = logs_dir / f"process-{document_id}-{stamp}.md"

    lines = [
        f"# Process-document log: {title or document_id}",
        "",
        f"- document_id: `{document_id}`",
        f"- run_at: {datetime.now(timezone.utc).isoformat()}",
        f"- candidates_extracted: {len(candidates)}",
        f"- debated: {len(debated)}",
        "",
        "## Candidate terms",
        "",
    ]
    for c in candidates:
        lines.append(f"- **{c.term}** — {c.context}")
    lines.append("")
    lines.append("## Debate outcomes")
    lines.append("")
    for d in debated:
        lines.append(f"### {d.get('term')}")
        lines.append("")
        lines.append(f"- outcome: `{d.get('outcome')}`")
        if "mpl_label" in d and d["mpl_label"]:
            lines.append(f"- mpl_label: `{d['mpl_label']}`")
        if "final_confidence" in d and d["final_confidence"] is not None:
            lines.append(f"- final_confidence: {d['final_confidence']}")
        if "iterations_used" in d:
            lines.append(f"- iterations_used: {d['iterations_used']}")
        if d.get("final_definition"):
            lines.append("")
            lines.append(f"  > {d['final_definition']}")
        if d.get("error"):
            lines.append(f"- error: {d['error']}")

        hr = d.get("hierarchy_review")
        if hr:
            lines.append("")
            lines.append("**Hierarchy review (multi-pass consensus):**")
            if "error" in hr:
                lines.append(f"- error: {hr['error']}")
            else:
                lines.append(f"- n_passes: {hr['n_passes']}")
                lines.append(
                    f"- consensus actions: {hr['consensus_actions_count']}"
                )
                if hr.get("status_counts"):
                    lines.append(
                        f"- status counts: {hr['status_counts']}"
                    )
                if hr.get("per_pass_proposals"):
                    lines.append("- per-pass proposals (for diagnostics):")
                    for i, pp in enumerate(hr["per_pass_proposals"], 1):
                        if not pp:
                            lines.append(f"  - pass {i}: (no actions)")
                            continue
                        for proposal in pp:
                            payload_summary = ", ".join(
                                f"{k}={v!r}" for k, v in proposal["payload"].items()
                            )
                            lines.append(
                                f"  - pass {i}: `{proposal['type']}`"
                                f"({payload_summary}) conf={proposal['conf']}"
                            )
                for a in hr.get("actions", []):
                    payload_summary = ", ".join(
                        f"{k}={v!r}" for k, v in a["payload"].items()
                    )
                    lines.append(
                        f"  - `{a['type']}`({payload_summary}) "
                        f"conf={a['confidence']} → **{a['status']}** "
                        f"({a['detail']})"
                    )
                    if a.get("reason"):
                        lines.append(f"    > {a['reason']}")
        lines.append("")

    log_path.write_text("\n".join(lines), encoding="utf-8")
    return log_path


def _list_proposals(config: AppConfig, *, status: str | None) -> int:
    from mahalath.db import close_all, get_database
    from mahalath.proposals import list_proposals

    try:
        db = get_database(config)
    except Exception as exc:  # pragma: no cover
        print(f"mahalath: MongoDB unreachable: {exc}", file=sys.stderr)
        return 4

    try:
        proposals = list_proposals(db, status=status)
        payload = {
            "count": len(proposals),
            "status_filter": status,
            "proposals": [_proposal_summary(p) for p in proposals],
        }
        print(json.dumps(payload, indent=2, default=str))
        return 0
    finally:
        close_all()


def _show_proposal(config: AppConfig, proposal_id: str) -> int:
    from mahalath.db import close_all, get_database
    from mahalath.proposals import ProposalError, get_proposal

    try:
        db = get_database(config)
    except Exception as exc:  # pragma: no cover
        print(f"mahalath: MongoDB unreachable: {exc}", file=sys.stderr)
        return 4

    try:
        proposal = get_proposal(proposal_id, db)
    except ProposalError as exc:
        print(f"mahalath: {exc}", file=sys.stderr)
        return 9
    finally:
        # Keep db open for the print below; close after.
        pass

    try:
        print(json.dumps(proposal.model_dump(), indent=2, default=str))
        return 0
    finally:
        close_all()


def _operator_decision(
    config: AppConfig, proposal_id: str, *, kind: str, note: str | None
) -> int:
    from mahalath.db import close_all, get_database
    from mahalath.proposals import (
        ProposalError,
        accept_proposal,
        reject_proposal,
        rollback_proposal,
    )

    try:
        db = get_database(config)
    except Exception as exc:  # pragma: no cover
        print(f"mahalath: MongoDB unreachable: {exc}", file=sys.stderr)
        return 4

    fn = {
        "accept": accept_proposal,
        "reject": reject_proposal,
        "rollback": rollback_proposal,
    }[kind]

    try:
        result = fn(proposal_id, db, note=note)
    except ProposalError as exc:
        print(f"mahalath: {exc}", file=sys.stderr)
        return 9
    finally:
        # close after we print success
        pass

    try:
        print(json.dumps({
            "ok": True,
            "proposal_id": result.proposal_id,
            "previous_status": result.previous_status,
            "new_status": result.new_status,
            "detail": result.detail,
            "application_result": result.application_result,
            "rollback_result": result.rollback_result,
        }, indent=2, default=str))
        return 0
    finally:
        close_all()


def _proposal_summary(p) -> dict[str, Any]:
    return {
        "proposal_id": p.proposal_id,
        "action_type": p.action_type,
        "payload": p.payload,
        "confidence": p.confidence,
        "status": p.status,
        "proposed_by": p.proposed_by,
        "reason": p.reason,
        "operator_decision": p.operator_decision,
        "operator_note": p.operator_note,
        "created_at": p.created_at,
    }


def _export_glossary(
    config: AppConfig, *, fmt: str, out_path: Path | None
) -> int:
    from mahalath.db import close_all, get_database
    from mahalath.glossary import export_json, export_markdown

    try:
        db = get_database(config)
    except Exception as exc:  # pragma: no cover
        print(f"mahalath: MongoDB unreachable: {exc}", file=sys.stderr)
        return 4

    try:
        if fmt == "md":
            result = export_markdown(
                db, out_path=out_path, database_name=config.mongo.database
            )
        else:
            result = export_json(
                db, out_path=out_path, database_name=config.mongo.database
            )

        if out_path is None:
            print(result.output)
        else:
            print(json.dumps({
                "ok": True,
                "format": result.format,
                "entry_count": result.entry_count,
                "written_to": str(result.written_to),
            }, indent=2))
        return 0
    finally:
        close_all()


def _list_ontology(config: AppConfig) -> int:
    from mahalath.db import close_all, get_database
    from mahalath.db.repositories import OntologyEntryRepository

    try:
        db = get_database(config)
    except Exception as exc:  # pragma: no cover
        print(f"mahalath: MongoDB unreachable: {exc}", file=sys.stderr)
        return 4

    try:
        repo = OntologyEntryRepository(db)
        labels = sorted(repo.all_labels())
        entries = []
        for label in labels:
            entry = repo.get(label)
            if entry is None:
                continue
            entries.append({
                "mpl_label": entry.mpl_label,
                "canonical_term": entry.canonical_term,
                "parent_label": entry.parent_label,
                "confidence": entry.confidence,
                "definition": entry.definitions[0].text if entry.definitions else None,
            })
        print(json.dumps({"count": len(entries), "entries": entries}, indent=2))
        return 0
    finally:
        close_all()


def _config_as_dict(config: AppConfig) -> dict[str, Any]:
    return json.loads(config.model_dump_json())


def _db_ping(config: AppConfig) -> int:
    try:
        from pymongo import MongoClient
        from pymongo.errors import PyMongoError
    except ImportError as exc:
        print(
            f"mahalath: pymongo is not installed in this environment: {exc}",
            file=sys.stderr,
        )
        return 3

    client = MongoClient(config.mongo.uri, serverSelectionTimeoutMS=2000)
    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        print(
            f"mahalath: MongoDB ping failed at {config.mongo.uri}: {exc}",
            file=sys.stderr,
        )
        return 4
    finally:
        client.close()

    print(
        json.dumps(
            {
                "ok": True,
                "uri": config.mongo.uri,
                "database": config.mongo.database,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
