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
    10 input directory missing (process-input)
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
    ingest_one.add_argument(
        "--style-overlay",
        default=None,
        help=(
            "Per-document style overlay path. Stored on the "
            "DocumentRecord and used by the pipeline in preference to "
            "the runtime-level style_overlay_path."
        ),
    )
    ingest_one.add_argument(
        "--language", default="en",
        help="Language lexicon this document feeds (ADR-028; default en).",
    )

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
    process_doc.add_argument(
        "--no-context-backfill",
        action="store_true",
        help="Skip the post-persist pass that context-tags any definition "
        "the debate left untagged (only runs if contexts are defined).",
    )
    process_doc.add_argument(
        "--no-intent-backfill",
        action="store_true",
        help="Skip the post-persist N-pass intent attribution on this "
        "run's accepted entries (only runs if an intent taxonomy exists).",
    )

    process_input_parser = subcommands.add_parser(
        "process-input",
        help="Ingest every Markdown file in input/ and run the pipeline "
        "on any document that has not yet been processed.",
    )
    process_input_parser.add_argument(
        "--max-terms",
        type=int,
        default=1,
        help="Max terms to debate per document (default: 1).",
    )
    process_input_parser.add_argument(
        "--no-hierarchy-review",
        action="store_true",
        help="Skip the post-accept hierarchy-review pass.",
    )
    process_input_parser.add_argument(
        "--consensus-passes",
        type=int,
        default=None,
        help="Override hierarchy_consensus_passes for this run.",
    )
    process_input_parser.add_argument(
        "--no-context-backfill",
        action="store_true",
        help="Skip the post-persist context-tagging pass on each document.",
    )
    process_input_parser.add_argument(
        "--no-intent-backfill",
        action="store_true",
        help="Skip the post-persist intent attribution on each document.",
    )
    retrieve_parser = subcommands.add_parser(
        "retrieve",
        help="Resolve human term(s) to codified MPL references and print "
        "their full meaning (all frames). For LLM-driven retrieval.",
    )
    retrieve_parser.add_argument(
        "terms", nargs="+", help="One or more human terms/phrases to resolve.",
    )
    retrieve_parser.add_argument(
        "--limit", type=int, default=10,
        help="Max matches to return (default 10).",
    )
    retrieve_parser.add_argument(
        "--branch", default=None,
        help="Restrict to entries under this ancestor MPL label.",
    )
    retrieve_parser.add_argument(
        "--context", default=None,
        help="Restrict to entries carrying a definition in this frame.",
    )
    retrieve_parser.add_argument(
        "--status", default=None, help="Restrict to entries with this status.",
    )
    retrieve_parser.add_argument(
        "--intent", default=None,
        help="Restrict to entries carrying a definition tagged with this "
        "intent (name or id), e.g. --intent teach.",
    )
    retrieve_parser.add_argument(
        "--min-confidence", type=float, default=None,
        help="Restrict to entries at or above this confidence.",
    )
    retrieve_parser.add_argument(
        "--language", default="en",
        help="Language lexicon to search (ADR-030; default en).",
    )
    retrieve_parser.add_argument(
        "--matches-only", action="store_true",
        help="Print ranked matches without building the bundle.",
    )
    retrieve_parser.add_argument(
        "--budget", type=int, default=1500,
        help="Token budget for the bundle (chars/4 estimate; default 1500).",
    )
    retrieve_parser.add_argument(
        "--format", choices=("json", "text"), default="json",
        help="Bundle output: full JSON (default) or the compact NL text.",
    )

    propose_term_parser = subcommands.add_parser(
        "propose-term",
        help="Propose a term the ontology doesn't confidently cover: "
        "returns existing matches if covered, otherwise enqueues it onto "
        "the undecided path for REM re-review / operator decision.",
    )
    propose_term_parser.add_argument(
        "term", help="The human term to propose.",
    )
    propose_term_parser.add_argument(
        "--context", default=None,
        help="Source snippet supporting the term (makes the queued item "
        "auto-debatable by REM re-review).",
    )
    propose_term_parser.add_argument(
        "--near", default=None,
        help="MPL label the term is believed related to (folded into the "
        "debate context as a hint).",
    )
    propose_term_parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would happen without enqueueing.",
    )

    propose_term_parser.add_argument(
        "--language", default="en",
        help="Language lexicon to check/enqueue against (default en).",
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
        parser_.add_argument(
            "--decided-via", choices=["operator", "claude_delegate"],
            default="operator",
            help="Who is rendering this verdict. Use claude_delegate "
            "when an LLM acts on delegated authority, so the §3.4 "
            "calibration metric can exclude it (default: operator).",
        )

    list_contexts_parser = subcommands.add_parser(
        "list-contexts",
        help="List definition contexts: meaning frames and intent tags.",
    )
    list_contexts_parser.add_argument(
        "--kind", choices=("frame", "intent"), default=None,
        help="Restrict to one taxonomy (default: both).",
    )
    add_context_parser = subcommands.add_parser(
        "add-context",
        help="Define a new definition context (e.g., theological, structural) "
        "or, with --kind intent, an intent tag (e.g., teach, persuade).",
    )
    add_context_parser.add_argument("name", help="Short tag, e.g. 'theological'.")
    add_context_parser.add_argument(
        "--description",
        required=True,
        help="One-sentence description of what definitions in this context mean.",
    )
    add_context_parser.add_argument(
        "--kind", choices=("frame", "intent"), default="frame",
        help="Taxonomy this row belongs to: a meaning frame (default) or "
        "an intent tag (ADR-024).",
    )
    seed_intents_parser = subcommands.add_parser(
        "seed-intents",
        help="Insert the standard intent taxonomy (teach, persuade, "
        "reassure, ...) idempotently. Operator edits afterwards via "
        "add-context --kind intent.",
    )
    seed_intents_parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be inserted without writing.",
    )
    show_context_parser = subcommands.add_parser(
        "show-context",
        help="Show one definition context by id or name.",
    )
    show_context_parser.add_argument("identifier")

    backfill_contexts_parser = subcommands.add_parser(
        "backfill-contexts",
        help="Walk untagged definitions and ask the model to propose a "
        "context for each. Defaults to dry-run; pass --apply to write.",
    )
    backfill_contexts_parser.add_argument(
        "--max-items", type=int, default=50,
        help="Maximum definitions to process this run (default 50).",
    )
    backfill_contexts_parser.add_argument(
        "--apply", action="store_true",
        help="Actually write the context_ids (otherwise dry-run + print).",
    )
    backfill_contexts_parser.add_argument(
        "--adapter", default=None,
        help="Adapter (default: runtime.model_adapter; ollama_cli for local).",
    )
    backfill_contexts_parser.add_argument(
        "--model", default=None,
        help="Override adapter default model.",
    )

    backfill_intents_parser = subcommands.add_parser(
        "backfill-intents",
        help="Walk unattributed definitions and run N-pass intent "
        "attribution (tags stored only on unanimity, ADR-025). Defaults "
        "to dry-run; pass --apply to write.",
    )
    backfill_intents_parser.add_argument(
        "--max-items", type=int, default=50,
        help="Maximum definitions to process this run (default 50).",
    )
    backfill_intents_parser.add_argument(
        "--apply", action="store_true",
        help="Actually write unanimous attributions (otherwise dry-run + print).",
    )
    backfill_intents_parser.add_argument(
        "--passes", type=int, default=None,
        help="Override runtime.intent_consensus_passes (default 3).",
    )
    backfill_intents_parser.add_argument(
        "--adapter", default=None,
        help="Adapter (default: runtime.model_adapter; ollama_cli for local).",
    )
    backfill_intents_parser.add_argument(
        "--model", default=None,
        help="Override adapter default model.",
    )

    subcommands.add_parser(
        "list-stale",
        help="List ontology entries flagged as stale (upstream changed).",
    )
    subcommands.add_parser(
        "backfill-references",
        help="One-shot migration: recompute references_labels for every "
        "ontology entry. Use on databases that pre-date S2.17.",
    )
    subcommands.add_parser(
        "backfill-language",
        help="One-shot migration: stamp language='en' on entries and "
        "documents that pre-date the M-A multilingual slice (ADR-028).",
    )

    seed_relations_parser = subcommands.add_parser(
        "seed-mapping-relations",
        help="Seed the standard cross-language relationship taxonomy "
        "(equivalent, partial_overlap, narrower_than, broader_than). "
        "Idempotent.",
    )
    seed_relations_parser.add_argument("--dry-run", action="store_true")

    genmap_parser = subcommands.add_parser(
        "generate-mappings",
        help="Scout candidate cross-language pairs and run the gated "
        "attribution (ADR-029). Dry-run by default; --apply stores.",
    )
    genmap_parser.add_argument("--source-language", required=True)
    genmap_parser.add_argument("--target-language", required=True)
    genmap_parser.add_argument("--max-items", type=int, default=20)
    genmap_parser.add_argument("--passes", type=int, default=None)
    genmap_parser.add_argument("--apply", action="store_true")
    genmap_parser.add_argument(
        "--adapter", default=None,
        help="Adapter name (default: runtime.model_adapter).",
    )

    listmap_parser = subcommands.add_parser(
        "list-mappings",
        help="List stored cross-language mappings.",
    )
    listmap_parser.add_argument("--status", default=None,
                                choices=["accepted", "rejected", "unresolved"])

    subcommands.add_parser(
        "backfill-paths",
        help="One-shot migration: recompute the materialised ancestor "
        "path for every ontology entry. Use on databases that pre-date "
        "the S-B retrieval slice.",
    )

    subtree_parser = subcommands.add_parser(
        "subtree",
        help="Print a limited-depth descendant summary rooted at an MPL "
        "label (operator FR-5).",
    )
    subtree_parser.add_argument(
        "label", help="The root MPL label to summarise descendants of.",
    )
    subtree_parser.add_argument(
        "--depth", type=int, default=1,
        help="How many levels of descendants to include (default 1).",
    )

    audit_stale_parser = subcommands.add_parser(
        "audit-stale",
        help="Walk list_stale, ask the model whether each entry's "
        "definition is still consistent; clear the flag on convergence.",
    )
    audit_stale_parser.add_argument(
        "--max-items", type=int, default=10,
        help="Items to audit per run (default 10).",
    )
    audit_stale_parser.add_argument(
        "--adapter", default=None,
        help="Adapter name (default: runtime.model_adapter from config).",
    )
    audit_stale_parser.add_argument(
        "--model", default=None,
        help="Override adapter default model.",
    )

    redefine_parser = subcommands.add_parser(
        "redefine-stale",
        help="For audit-flagged stale entries, ask the model for a fresh "
        "definition consistent with current upstream; append + clear stale.",
    )
    redefine_parser.add_argument(
        "--max-items", type=int, default=10,
        help="Items to redefine per run (default 10).",
    )
    redefine_parser.add_argument(
        "--adapter", default=None,
        help="Adapter name (default: runtime.model_adapter).",
    )
    redefine_parser.add_argument(
        "--model", default=None,
        help="Override adapter default model.",
    )
    redefine_parser.add_argument(
        "--min-confidence", type=float, default=6.0,
        help="Don't write definitions below this confidence (default 6.0).",
    )
    redefine_parser.add_argument(
        "--no-intent-backfill", action="store_true",
        help="Skip the scoped intent attribution that normally runs "
        "over the entries this pass redefined.",
    )

    frontier_parser = subcommands.add_parser(
        "frontier-review",
        help="Adjudicate pending_review proposals via a frontier model. "
        "Requires ANTHROPIC_API_KEY env var when using claude_api.",
    )
    frontier_parser.add_argument(
        "--max-items", type=int, default=25,
        help="Cap on items adjudicated this run (default 25).",
    )
    frontier_parser.add_argument(
        "--adapter", default="claude_api",
        help="Adapter name (default claude_api). Any name make_adapter "
        "recognises is allowed.",
    )
    frontier_parser.add_argument(
        "--model", default=None,
        help="Override the adapter's default model.",
    )

    serve_parser = subcommands.add_parser(
        "serve",
        help="Run the FastAPI web UI (read-only ontology browser + "
        "proposal accept/reject). Requires the optional [web] extra.",
    )
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)
    serve_parser.add_argument(
        "--reload", action="store_true",
        help="Enable auto-reload (development only).",
    )

    run_parser = subcommands.add_parser(
        "run",
        help="Run the polling scheduler: process-input on an interval, "
        "REM job on cron. Blocks in the foreground until Ctrl-C.",
    )
    run_parser.add_argument(
        "--once",
        action="store_true",
        help="Fire both jobs once and exit (cron-style external scheduling).",
    )
    run_parser.add_argument(
        "--poll-seconds",
        type=int,
        default=None,
        help="Override ingestion.poll_interval_seconds for this run.",
    )
    run_parser.add_argument(
        "--rem-cron",
        default=None,
        help="Override rem.cron for this run.",
    )

    effectiveness_parser = subcommands.add_parser(
        "effectiveness",
        help="Self-analysis of decision-making effectiveness (§3.4): "
        "debate outcomes, operator-vs-confidence calibration, queue "
        "health, re-debate resolution, review yield.",
    )
    effectiveness_parser.add_argument(
        "--format", choices=["text", "json"], default="text",
        help="Output format (default: text).",
    )
    effectiveness_parser.add_argument(
        "--snapshot", action="store_true",
        help="Also append the report as one JSON line to "
        "logs/effectiveness.jsonl (what the nightly REM job does).",
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
        return _ingest_one(
            config, Path(args.path), style_overlay_path=args.style_overlay,
            language=args.language,
        )

    if args.command == "process-document":
        return _process_document(
            config,
            args.document_id,
            max_terms=args.max_terms,
            skip_hierarchy_review=args.no_hierarchy_review,
            consensus_passes_override=args.consensus_passes,
            skip_context_backfill=args.no_context_backfill,
            skip_intent_backfill=args.no_intent_backfill,
        )

    if args.command == "propose-term":
        return _propose_term(
            config,
            term=args.term,
            context=args.context,
            near=args.near,
            dry_run=args.dry_run,
            language=args.language,
        )

    if args.command == "list-ontology":
        return _list_ontology(config)

    if args.command == "retrieve":
        return _retrieve(
            config,
            terms=args.terms,
            language=args.language,
            limit=args.limit,
            branch=args.branch,
            context=args.context,
            status=args.status,
            min_confidence=args.min_confidence,
            intent=args.intent,
            matches_only=args.matches_only,
            budget=args.budget,
            out_format=args.format,
        )

    if args.command == "list-proposals":
        return _list_proposals(config, status=args.status)

    if args.command == "show-proposal":
        return _show_proposal(config, args.proposal_id)

    if args.command == "accept-proposal":
        return _operator_decision(
            config, args.proposal_id, kind="accept", note=args.note,
            decided_via=args.decided_via,
        )

    if args.command == "reject-proposal":
        return _operator_decision(
            config, args.proposal_id, kind="reject", note=args.note,
            decided_via=args.decided_via,
        )

    if args.command == "rollback-proposal":
        return _operator_decision(
            config, args.proposal_id, kind="rollback", note=args.note,
            decided_via=args.decided_via,
        )

    if args.command == "effectiveness":
        return _effectiveness(
            config, out_format=args.format, snapshot=args.snapshot
        )

    if args.command == "export-glossary":
        return _export_glossary(
            config, fmt=args.format,
            out_path=Path(args.out) if args.out else None,
        )

    if args.command == "run":
        from mahalath.scheduler import run_scheduler
        import logging
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )
        return run_scheduler(
            config,
            once=args.once,
            poll_seconds=args.poll_seconds,
            rem_cron=args.rem_cron,
        )

    if args.command == "list-contexts":
        return _list_contexts(config, kind=args.kind)

    if args.command == "add-context":
        return _add_context(
            config, name=args.name, description=args.description,
            kind=args.kind,
        )

    if args.command == "seed-intents":
        return _seed_intents(config, dry_run=args.dry_run)

    if args.command == "show-context":
        return _show_context(config, identifier=args.identifier)

    if args.command == "backfill-contexts":
        return _backfill_contexts(
            config,
            max_items=args.max_items,
            apply_flag=args.apply,
            adapter_name=args.adapter,
            model_override=args.model,
        )

    if args.command == "backfill-intents":
        return _backfill_intents(
            config,
            max_items=args.max_items,
            apply_flag=args.apply,
            passes_override=args.passes,
            adapter_name=args.adapter,
            model_override=args.model,
        )

    if args.command == "list-stale":
        return _list_stale(config)

    if args.command == "backfill-references":
        return _backfill_references(config)

    if args.command == "seed-mapping-relations":
        return _seed_mapping_relations(config, dry_run=args.dry_run)

    if args.command == "generate-mappings":
        return _generate_mappings(
            config,
            source_language=args.source_language,
            target_language=args.target_language,
            max_items=args.max_items,
            passes=args.passes,
            apply_flag=args.apply,
            adapter_name=args.adapter,
        )

    if args.command == "list-mappings":
        return _list_mappings(config, status=args.status)

    if args.command == "backfill-language":
        return _backfill_language(config)

    if args.command == "backfill-paths":
        return _backfill_paths(config)

    if args.command == "subtree":
        return _subtree(config, label=args.label, depth=args.depth)

    if args.command == "audit-stale":
        return _audit_stale(
            config,
            max_items=args.max_items,
            adapter_name=args.adapter,
            model_override=args.model,
        )

    if args.command == "redefine-stale":
        return _redefine_stale(
            config,
            max_items=args.max_items,
            adapter_name=args.adapter,
            model_override=args.model,
            min_confidence=args.min_confidence,
            skip_intent_backfill=args.no_intent_backfill,
        )

    if args.command == "frontier-review":
        return _frontier_review(
            config,
            max_items=args.max_items,
            adapter_name=args.adapter,
            model_override=args.model,
        )

    if args.command == "serve":
        try:
            import uvicorn
        except ImportError:
            print(
                "mahalath: serve requires the optional [web] extras; "
                "install with: pip install -e \".[web]\"",
                file=sys.stderr,
            )
            return 11
        from mahalath.web.app import create_app
        import logging
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )
        app = create_app(config)
        uvicorn.run(
            app, host=args.host, port=args.port, reload=args.reload, log_level="info",
        )
        return 0

    if args.command == "process-input":
        return _process_input(
            config,
            max_terms_per_doc=args.max_terms,
            skip_hierarchy_review=args.no_hierarchy_review,
            consensus_passes_override=args.consensus_passes,
            skip_context_backfill=args.no_context_backfill,
            skip_intent_backfill=args.no_intent_backfill,
        )

    if args.command == "debate-one":
        print(
            "mahalath: command 'debate-one' is not yet implemented.",
            file=sys.stderr,
        )
        return 2

    parser.print_help()
    return 0


def _ingest_one(
    config: AppConfig,
    source_path: Path,
    *,
    style_overlay_path: str | None = None,
    language: str = "en",
) -> int:
    from mahalath.db import close_all, ensure_indexes, get_database
    from mahalath.ingestion import IngestionError, ingest_one

    try:
        db = get_database(config)
        ensure_indexes(db)
    except Exception as exc:  # pragma: no cover - exercised by db-ping path
        print(f"mahalath: MongoDB unreachable: {exc}", file=sys.stderr)
        return 4

    try:
        result = ingest_one(
            source_path, config, db, style_overlay_path=style_overlay_path,
            language=language,
        )
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
        "style_overlay_path": result.document.style_overlay_path,
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
    skip_context_backfill: bool = False,
    skip_intent_backfill: bool = False,
) -> int:
    from mahalath.adapters import make_adapter
    from mahalath.db import close_all, ensure_indexes, get_database
    from mahalath.db.repositories import DocumentRepository

    try:
        db = get_database(config)
        ensure_indexes(db)
    except Exception as exc:  # pragma: no cover
        print(f"mahalath: MongoDB unreachable: {exc}", file=sys.stderr)
        return 4

    try:
        document = DocumentRepository(db).find_by_document_id(document_id)
        if document is None:
            print(f"mahalath: document not found: {document_id}", file=sys.stderr)
            return 6

        adapter = make_adapter(config.runtime.model_adapter, config)
        from mahalath.style import resolve_style_overlay
        style_overlay = resolve_style_overlay(document, config)

        result = _run_pipeline_on_document(
            document, db, adapter, config,
            max_terms=max_terms,
            skip_hierarchy_review=skip_hierarchy_review,
            consensus_passes_override=consensus_passes_override,
            style_overlay=style_overlay,
            skip_context_backfill=skip_context_backfill,
            skip_intent_backfill=skip_intent_backfill,
        )

        if not result.get("ok"):
            print(f"mahalath: {result.get('error')}", file=sys.stderr)
            error_text = result.get("error", "")
            if "archived source missing" in error_text:
                return 7
            if "extraction failed" in error_text:
                return 8
            return 9

        print(json.dumps(result, indent=2))
        return 0
    finally:
        close_all()


def _process_input(
    config: AppConfig,
    *,
    max_terms_per_doc: int,
    skip_hierarchy_review: bool = False,
    consensus_passes_override: int | None = None,
    skip_context_backfill: bool = False,
    skip_intent_backfill: bool = False,
) -> int:
    from mahalath.adapters import make_adapter
    from mahalath.db import close_all, ensure_indexes, get_database
    from mahalath.ingestion import IngestionError, ingest_one
    from mahalath.style import resolve_style_overlay

    try:
        db = get_database(config)
        ensure_indexes(db)
    except Exception as exc:  # pragma: no cover
        print(f"mahalath: MongoDB unreachable: {exc}", file=sys.stderr)
        return 4

    try:
        input_dir = Path(config.paths.input)
        if not input_dir.is_absolute():
            input_dir = Path.cwd() / input_dir
        if not input_dir.exists():
            print(
                f"mahalath: input directory missing: {input_dir}",
                file=sys.stderr,
            )
            return 10

        files = sorted(input_dir.glob("*.md"))
        if not files:
            print(json.dumps({
                "ok": True,
                "input_directory": str(input_dir),
                "files_scanned": 0,
                "results": [],
            }, indent=2))
            return 0

        adapter = make_adapter(config.runtime.model_adapter, config)

        results: list[dict[str, Any]] = []
        for file_path in files:
            entry: dict[str, Any] = {"file": str(file_path)}
            try:
                ingestion = ingest_one(file_path, config, db)
            except IngestionError as exc:
                entry["ok"] = False
                entry["error"] = f"ingestion failed: {exc}"
                results.append(entry)
                continue

            entry["document_id"] = ingestion.document.document_id
            entry["duplicate"] = ingestion.duplicate
            entry["title"] = ingestion.document.title

            # Skip pipeline work if this document has already been processed
            # (idempotent re-runs on a watched folder are the headline use).
            if ingestion.document.processed_at is not None:
                entry["ok"] = True
                entry["skipped"] = "already processed"
                results.append(entry)
                continue

            doc_overlay = resolve_style_overlay(ingestion.document, config)
            pipeline_result = _run_pipeline_on_document(
                ingestion.document, db, adapter, config,
                max_terms=max_terms_per_doc,
                skip_hierarchy_review=skip_hierarchy_review,
                consensus_passes_override=consensus_passes_override,
                style_overlay=doc_overlay,
                skip_context_backfill=skip_context_backfill,
                skip_intent_backfill=skip_intent_backfill,
            )
            # Avoid double-keying document_id / title.
            for k, v in pipeline_result.items():
                if k not in entry:
                    entry[k] = v
            results.append(entry)

        successes = sum(
            1 for r in results
            if r.get("ok", False) and "skipped" not in r
        )
        skipped = sum(1 for r in results if r.get("skipped"))
        failed = sum(1 for r in results if not r.get("ok", True))
        payload = {
            "ok": True,
            "input_directory": str(input_dir),
            "files_scanned": len(files),
            "processed": successes,
            "skipped": skipped,
            "failed": failed,
            "results": results,
        }
        print(json.dumps(payload, indent=2))
        return 0
    finally:
        close_all()


def _run_pipeline_on_document(
    document,
    db,
    adapter,
    config: AppConfig,
    *,
    max_terms: int,
    skip_hierarchy_review: bool,
    consensus_passes_override: int | None,
    style_overlay: str | None,
    skip_context_backfill: bool = False,
    skip_intent_backfill: bool = False,
) -> dict[str, Any]:
    """Run extract → debate → persist → hierarchy review on one document.

    Returns a result dict in the same shape as `_process_document`'s
    JSON payload. The `ok: False` branch carries an `error` string and
    the caller maps it to the appropriate exit code (archive missing,
    extraction failure, etc.).
    """
    from mahalath.actions import dispatch
    from mahalath.adapters.base import AdapterError
    from mahalath.chunking import extract_candidates_chunked
    from mahalath.db.repositories import DocumentRepository
    from mahalath.debate import DebateError, run_debate
    from mahalath.extraction import ExtractionError
    from mahalath.hierarchy import (
        HierarchyReviewError,
        run_hierarchy_review_consensus,
    )
    from mahalath.ontology import persist_debate_result

    archive_path = Path(document.archive_path)
    if not archive_path.is_absolute():
        archive_path = Path.cwd() / archive_path
    if not archive_path.exists():
        return {
            "ok": False,
            "error": f"archived source missing: {archive_path}",
            "document_id": document.document_id,
        }
    text = archive_path.read_text(encoding="utf-8", errors="replace")

    try:
        candidates = extract_candidates_chunked(
            text, adapter, style_overlay=style_overlay
        )
    except ExtractionError as exc:
        return {
            "ok": False,
            "error": f"extraction failed: {exc}",
            "document_id": document.document_id,
        }

    # Snapshot the definition contexts once per document so each debate
    # call sees the live frame options. Frames only (ADR-024) — intent
    # tags are not meaning frames.
    from mahalath.db.repositories import DefinitionContextRepository
    available_contexts = [
        {"name": c.name, "description": c.description}
        for c in DefinitionContextRepository(db).all(kind="frame")
    ]

    # Known-term guard (S2.41): a lexicon hosts many documents, so a
    # candidate that already exists as a canonical term or alias is
    # provenance, not a new concept — record this document on the
    # existing entry's source_document_ids and skip the debate (no
    # model spend, no duplicate MPL label). Known matches don't consume
    # max_terms slots.
    doc_language = getattr(document, "language", None) or "en"
    known_by_term: dict[str, str] = {}
    for doc_ in db.ontology_entries.find(
        {"language": doc_language}, {"canonical_term": 1, "aliases": 1}
    ):
        known_by_term[doc_["canonical_term"].casefold()] = doc_["_id"]
        for alias in doc_.get("aliases") or []:
            known_by_term.setdefault(alias.casefold(), doc_["_id"])

    debated: list[dict[str, Any]] = []
    accepted_labels: set[str] = set()
    debates_run = 0
    for candidate in candidates:
        existing_label = known_by_term.get(candidate.term.casefold())
        if existing_label is not None:
            db.ontology_entries.update_one(
                {"_id": existing_label},
                {"$addToSet": {
                    "source_document_ids": document.document_id,
                }},
            )
            debated.append({
                "term": candidate.term,
                "outcome": "already_known",
                "mpl_label": existing_label,
            })
            continue
        if debates_run >= max_terms:
            # Slots exhausted: stop debating, but keep sweeping the
            # remaining candidates for known-term provenance (free).
            continue
        debates_run += 1
        try:
            debate_result = run_debate(
                term=candidate.term,
                context=candidate.context,
                source_document_id=document.document_id,
                adapter=adapter,
                runtime=config.runtime,
                style_overlay=style_overlay,
                available_contexts=available_contexts or None,
            )
        except (DebateError, AdapterError) as exc:
            debated.append({
                "term": candidate.term,
                "outcome": "error",
                "error": str(exc),
            })
            continue

        persist_result = persist_debate_result(
            debate_result, db, config.runtime
        )
        term_record: dict[str, Any] = {
            "term": candidate.term,
            "outcome": debate_result.outcome,
            "final_confidence": debate_result.final_confidence,
            "final_definition": debate_result.final_definition,
            "iterations_used": debate_result.iterations_used,
            "mpl_label": persist_result.mpl_label,
            "decision_log_id": debate_result.decision_log_id,
        }
        if (
            persist_result.outcome == "accepted"
            and persist_result.mpl_label is not None
        ):
            accepted_labels.add(persist_result.mpl_label)
            # A duplicate candidate later in this run is provenance,
            # not a second debate.
            known_by_term.setdefault(
                candidate.term.casefold(), persist_result.mpl_label
            )
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
                style_overlay=style_overlay,
            )
        debated.append(term_record)

    DocumentRepository(db).mark_processed(document.document_id)
    log_path = _write_process_log(
        config, document.document_id, document.title, candidates, debated
    )

    # Catch-up context tagging: the debate already proposes a context per
    # definition, but the model sometimes returns null or a name that
    # doesn't resolve. Scope the backfill to the entries this run touched
    # so it stays bounded — it is not a whole-database sweep (that is the
    # standalone `backfill-contexts` command's job).
    context_backfill: dict[str, Any] | None = None
    if not skip_context_backfill and accepted_labels:
        from mahalath.staleness import backfill_definition_contexts
        backfill = backfill_definition_contexts(
            config, db, adapter,
            max_items=len(accepted_labels) * 8 or 50,
            apply=True,
            only_labels=accepted_labels,
        )
        context_backfill = {
            "applied": backfill.applied,
            "skipped": backfill.skipped,
            "errored": backfill.errored,
            "proposals": [
                {
                    "mpl_label": p.mpl_label,
                    "definition_index": p.definition_index,
                    "context": p.proposed_context_name,
                    "source": p.source,
                }
                for p in backfill.proposals
            ],
        }

    # Intent attribution (I-B, ADR-025): N-pass unanimity-gated tagging
    # of this run's accepted entries. All model-sourced intent goes
    # through this gate — the debate contract deliberately does NOT
    # emit intent (a single in-debate sample can't satisfy unanimity).
    # No-ops when no intent taxonomy is defined.
    intent_backfill: dict[str, Any] | None = None
    if not skip_intent_backfill and accepted_labels:
        from mahalath.intents import backfill_intents
        ib = backfill_intents(
            db, adapter,
            max_items=len(accepted_labels) * 8 or 50,
            passes=config.runtime.intent_consensus_passes,
            min_confidence=config.runtime.confidence_threshold,
            apply=True,
            only_labels=accepted_labels,
            style_overlay=style_overlay,
            models=config.runtime.consensus_models,
        )
        intent_backfill = {
            "attempted": ib.attempted,
            "stored": ib.stored,
            "below_threshold": ib.below_threshold,
            "no_unanimous": ib.no_unanimous,
            "errored": ib.errored,
            "attributions": [
                {
                    "mpl_label": a.mpl_label,
                    "definition_index": a.definition_index,
                    "tags": a.unanimous_tags,
                    "intentionality": a.intentionality,
                    "intent_confidence": a.intent_confidence,
                    "outcome": a.outcome,
                }
                for a in ib.attributions
            ],
        }

    glossary_paths: dict[str, str] | None = None
    if any(d.get("outcome") == "accepted" for d in debated):
        from mahalath.glossary import refresh_glossary
        exports = refresh_glossary(config, db)
        glossary_paths = {
            fmt: str(exports[fmt].written_to) for fmt in exports
        }

    return {
        "ok": True,
        "document_id": document.document_id,
        "title": document.title,
        "candidates_extracted": len(candidates),
        "debated": debated,
        "remaining_candidates": [c.term for c in candidates[max_terms:]],
        "activity_log_path": str(log_path),
        "context_backfill": context_backfill,
        "intent_backfill": intent_backfill,
        "glossary_refreshed": glossary_paths,
    }


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
    style_overlay: str | None = None,
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
            style_overlay=style_overlay,
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
    config: AppConfig, proposal_id: str, *, kind: str, note: str | None,
    decided_via: str = "operator",
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
        result = fn(proposal_id, db, note=note, decided_via=decided_via)
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


def _list_contexts(config: AppConfig, *, kind: str | None = None) -> int:
    from mahalath.db import close_all, get_database, DefinitionContextRepository

    try:
        db = get_database(config)
    except Exception as exc:  # pragma: no cover
        print(f"mahalath: MongoDB unreachable: {exc}", file=sys.stderr)
        return 4

    try:
        contexts = DefinitionContextRepository(db).all(kind=kind)
        payload = {
            "count": len(contexts),
            "kind_filter": kind,
            "contexts": [
                {
                    "context_id": c.context_id,
                    "name": c.name,
                    "kind": c.kind,
                    "description": c.description,
                    "created_by": c.created_by,
                    "created_at": str(c.created_at),
                }
                for c in contexts
            ],
        }
        print(json.dumps(payload, indent=2))
        return 0
    finally:
        close_all()


def _add_context(
    config: AppConfig, *, name: str, description: str, kind: str = "frame"
) -> int:
    from mahalath.db import close_all, ensure_indexes, get_database, DefinitionContextRepository
    from mahalath.db.models import DefinitionContext

    try:
        db = get_database(config)
        ensure_indexes(db)
    except Exception as exc:  # pragma: no cover
        print(f"mahalath: MongoDB unreachable: {exc}", file=sys.stderr)
        return 4

    try:
        repo = DefinitionContextRepository(db)
        existing = repo.get_by_name(name)
        if existing is not None:
            print(
                f"mahalath: a context named {name!r} already exists "
                f"(kind={existing.kind}, id={existing.context_id}). Names "
                "are one namespace across frames and intents.",
                file=sys.stderr,
            )
            return 13
        ctx = DefinitionContext(
            name=name, description=description, kind=kind,
            created_by="operator",
        )
        repo.insert(ctx)
        print(json.dumps({
            "ok": True,
            "context_id": ctx.context_id,
            "name": ctx.name,
            "kind": ctx.kind,
            "description": ctx.description,
        }, indent=2))
        return 0
    finally:
        close_all()


def _seed_intents(config: AppConfig, *, dry_run: bool) -> int:
    from mahalath.db import close_all, ensure_indexes, get_database
    from mahalath.intents import seed_intents

    try:
        db = get_database(config)
        ensure_indexes(db)
    except Exception as exc:  # pragma: no cover
        print(f"mahalath: MongoDB unreachable: {exc}", file=sys.stderr)
        return 4

    try:
        result = seed_intents(db, dry_run=dry_run)
        print(json.dumps(
            {"ok": True, "dry_run": dry_run, **result.to_dict()}, indent=2,
        ))
        return 0
    finally:
        close_all()


def _backfill_intents(
    config: AppConfig,
    *,
    max_items: int,
    apply_flag: bool,
    passes_override: int | None,
    adapter_name: str | None,
    model_override: str | None,
) -> int:
    import logging
    from mahalath.adapters import make_adapter
    from mahalath.adapters.base import AdapterError
    from mahalath.db import close_all, get_database
    from mahalath.intents import backfill_intents
    from mahalath.style import load_style_overlay

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    effective_adapter = adapter_name or config.runtime.model_adapter
    try:
        adapter = make_adapter(effective_adapter, config)
    except AdapterError as exc:
        print(f"mahalath: {exc}", file=sys.stderr)
        return 12

    if model_override:
        adapter.default_model = model_override

    passes = passes_override or config.runtime.intent_consensus_passes

    try:
        db = get_database(config)
        result = backfill_intents(
            db, adapter,
            max_items=max_items,
            passes=passes,
            min_confidence=config.runtime.confidence_threshold,
            apply=apply_flag,
            style_overlay=load_style_overlay(config),
            models=config.runtime.consensus_models,
        )
        if apply_flag and result.stored > 0:
            # The glossary export carries intent fields (I-C); keep the
            # artifacts current, mirroring backfill-contexts.
            from mahalath.glossary import refresh_glossary
            refresh_glossary(config, db)
    finally:
        close_all()

    print(json.dumps({
        "ok": True,
        "dry_run": not apply_flag,
        "adapter": effective_adapter,
        "model": getattr(adapter, "default_model", None),
        "passes": passes,
        **result.to_dict(),
    }, indent=2))
    return 0


def _show_context(config: AppConfig, *, identifier: str) -> int:
    from mahalath.db import close_all, get_database, DefinitionContextRepository

    try:
        db = get_database(config)
    except Exception as exc:  # pragma: no cover
        print(f"mahalath: MongoDB unreachable: {exc}", file=sys.stderr)
        return 4

    try:
        repo = DefinitionContextRepository(db)
        ctx = repo.get(identifier) or repo.get_by_name(identifier)
        if ctx is None:
            print(f"mahalath: no context with id or name {identifier!r}", file=sys.stderr)
            return 14
        print(json.dumps(ctx.model_dump(), indent=2, default=str))
        return 0
    finally:
        close_all()


def _backfill_contexts(
    config: AppConfig,
    *,
    max_items: int,
    apply_flag: bool,
    adapter_name: str | None,
    model_override: str | None,
) -> int:
    import logging
    from mahalath.adapters import make_adapter
    from mahalath.adapters.base import AdapterError
    from mahalath.db import close_all, get_database
    from mahalath.staleness import backfill_definition_contexts

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    effective_adapter = adapter_name or config.runtime.model_adapter
    try:
        adapter = make_adapter(effective_adapter, config)
    except AdapterError as exc:
        print(f"mahalath: {exc}", file=sys.stderr)
        return 12

    if model_override:
        adapter.default_model = model_override

    try:
        db = get_database(config)
        result = backfill_definition_contexts(
            config, db, adapter,
            max_items=max_items, apply=apply_flag,
        )
        if apply_flag and result.applied > 0:
            from mahalath.glossary import refresh_glossary
            refresh_glossary(config, db)
    finally:
        close_all()

    payload = {
        "ok": True,
        "dry_run": not apply_flag,
        "adapter": effective_adapter,
        "model": getattr(adapter, "default_model", None),
        "untagged_at_start": result.untagged_at_start,
        "proposals_generated": result.proposals_generated,
        "applied": result.applied,
        "skipped": result.skipped,
        "errored": result.errored,
        "proposals": [
            {
                "mpl_label": p.mpl_label,
                "canonical_term": p.canonical_term,
                "definition_index": p.definition_index,
                "definition_model_used": p.definition_model_used,
                "definition_text_preview": p.definition_text[:140],
                "proposed_context_name": p.proposed_context_name,
                "source": p.source,
            }
            for p in result.proposals
        ],
        "errors": result.errors,
    }
    print(json.dumps(payload, indent=2))
    return 0


def _list_stale(config: AppConfig) -> int:
    from mahalath.db import close_all, get_database
    from mahalath.staleness import list_stale

    try:
        db = get_database(config)
    except Exception as exc:  # pragma: no cover
        print(f"mahalath: MongoDB unreachable: {exc}", file=sys.stderr)
        return 4

    try:
        stale = list_stale(db)
        payload = {
            "count": len(stale),
            "entries": [
                {
                    "mpl_label": e.mpl_label,
                    "canonical_term": e.canonical_term,
                    "references_labels": e.references_labels,
                    "stale_reasons": [
                        {**r, "changed_at": str(r.get("changed_at"))}
                        for r in e.stale_reasons
                    ],
                }
                for e in stale
            ],
        }
        print(json.dumps(payload, indent=2))
        return 0
    finally:
        close_all()


def _effectiveness(
    config: AppConfig, *, out_format: str, snapshot: bool
) -> int:
    from mahalath.analysis import (
        build_effectiveness_report,
        render_report_lines,
        report_to_dict,
        write_effectiveness_snapshot,
    )
    from mahalath.db import close_all, get_database

    try:
        db = get_database(config)
    except Exception as exc:  # pragma: no cover
        print(f"mahalath: MongoDB unreachable: {exc}", file=sys.stderr)
        return 4

    try:
        report = build_effectiveness_report(
            db, database_name=config.mongo.database
        )
        if out_format == "json":
            print(json.dumps(report_to_dict(report), indent=2))
        else:
            print("\n".join(render_report_lines(report)))
        if snapshot:
            path = write_effectiveness_snapshot(config, report)
            print(f"\nsnapshot appended: {path}", file=sys.stderr)
        return 0
    finally:
        close_all()


def _backfill_references(config: AppConfig) -> int:
    from mahalath.db import close_all, ensure_indexes, get_database
    from mahalath.staleness import backfill_references

    try:
        db = get_database(config)
        ensure_indexes(db)
    except Exception as exc:  # pragma: no cover
        print(f"mahalath: MongoDB unreachable: {exc}", file=sys.stderr)
        return 4

    try:
        result = backfill_references(db)
        print(json.dumps({"ok": True, **result}, indent=2))
        return 0
    finally:
        close_all()


def _seed_mapping_relations(config: AppConfig, *, dry_run: bool) -> int:
    from mahalath.db import close_all, get_database
    from mahalath.mappings import seed_mapping_relations

    try:
        db = get_database(config)
    except Exception as exc:  # pragma: no cover
        print(f"mahalath: MongoDB unreachable: {exc}", file=sys.stderr)
        return 4
    try:
        result = seed_mapping_relations(db, dry_run=dry_run)
        print(json.dumps({"ok": True, "dry_run": dry_run, **result}, indent=2))
        return 0
    finally:
        close_all()


def _generate_mappings(
    config: AppConfig,
    *,
    source_language: str,
    target_language: str,
    max_items: int,
    passes: int | None,
    apply_flag: bool,
    adapter_name: str | None,
) -> int:
    import logging as _logging
    from mahalath.adapters import make_adapter
    from mahalath.adapters.base import AdapterError
    from mahalath.db import close_all, ensure_indexes, get_database
    from mahalath.mappings import generate_mappings

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    effective_adapter = adapter_name or config.runtime.model_adapter
    try:
        adapter = make_adapter(effective_adapter, config)
    except AdapterError as exc:
        print(f"mahalath: {exc}", file=sys.stderr)
        return 12

    try:
        db = get_database(config)
        ensure_indexes(db)
        result = generate_mappings(
            config, db, adapter,
            source_language=source_language,
            target_language=target_language,
            max_items=max_items,
            passes=passes,
            apply=apply_flag,
        )
        print(json.dumps({
            "ok": True,
            "applied": apply_flag,
            "source_language": source_language,
            "target_language": target_language,
            **result.to_dict(),
        }, indent=2, default=str))
        return 0
    finally:
        close_all()


def _list_mappings(config: AppConfig, *, status: str | None) -> int:
    from mahalath.db import close_all, get_database
    from mahalath.db.repositories import MappingRepository

    try:
        db = get_database(config)
    except Exception as exc:  # pragma: no cover
        print(f"mahalath: MongoDB unreachable: {exc}", file=sys.stderr)
        return 4
    try:
        repo = MappingRepository(db)
        items = repo.by_status(status) if status else repo.all()
        print(json.dumps({
            "ok": True,
            "count": len(items),
            "mappings": [
                {
                    "mapping_id": m.mapping_id,
                    "pair": f"{m.source_label} ({m.source_language}) -> "
                            f"{m.target_label} ({m.target_language})",
                    "relationship": m.relationship,
                    "confidence": m.confidence,
                    "status": m.status,
                    "is_stale": m.is_stale,
                    "rationale": m.rationale,
                    "illocution_comparison": m.illocution_comparison,
                }
                for m in items
            ],
        }, indent=2, default=str))
        return 0
    finally:
        close_all()


def _backfill_language(config: AppConfig) -> int:
    """Stamp the default lexicon on pre-M-A records. Idempotent."""
    from mahalath.db import close_all, get_database

    try:
        db = get_database(config)
    except Exception as exc:  # pragma: no cover
        print(f"mahalath: MongoDB unreachable: {exc}", file=sys.stderr)
        return 4

    try:
        from mahalath.ontology import backfill_language
        print(json.dumps({"ok": True, **backfill_language(db)}, indent=2))
        return 0
    finally:
        close_all()


def _backfill_paths(config: AppConfig) -> int:
    from mahalath.db import close_all, get_database
    from mahalath.paths import backfill_paths

    try:
        db = get_database(config)
    except Exception as exc:  # pragma: no cover
        print(f"mahalath: MongoDB unreachable: {exc}", file=sys.stderr)
        return 4

    try:
        result = backfill_paths(db)
        print(json.dumps({"ok": True, **result}, indent=2))
        return 0
    finally:
        close_all()


def _subtree(config: AppConfig, *, label: str, depth: int) -> int:
    from mahalath.db import close_all, get_database
    from mahalath.retrieval import subtree

    try:
        db = get_database(config)
    except Exception as exc:  # pragma: no cover
        print(f"mahalath: MongoDB unreachable: {exc}", file=sys.stderr)
        return 4

    try:
        summary = subtree(db, label, depth=depth)
        if summary is None:
            print(
                json.dumps({"ok": False, "error": f"unknown label {label!r}"}),
                file=sys.stderr,
            )
            return 1
        print(json.dumps({"ok": True, **summary.to_dict()}, indent=2))
        return 0
    finally:
        close_all()


def _audit_stale(
    config: AppConfig,
    *,
    max_items: int,
    adapter_name: str | None,
    model_override: str | None,
) -> int:
    import logging
    from mahalath.adapters import make_adapter
    from mahalath.adapters.base import AdapterError
    from mahalath.db import close_all, get_database
    from mahalath.staleness import audit_pending_stale

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    effective_adapter = adapter_name or config.runtime.model_adapter
    try:
        adapter = make_adapter(effective_adapter, config)
    except AdapterError as exc:
        print(f"mahalath: {exc}", file=sys.stderr)
        return 12

    if model_override:
        adapter.default_model = model_override

    try:
        db = get_database(config)
        result = audit_pending_stale(config, db, adapter, max_items=max_items)
        # Refresh glossary if anything cleared (status changed).
        if result.items_cleared > 0:
            from mahalath.glossary import refresh_glossary
            refresh_glossary(config, db)
    finally:
        close_all()

    payload = {
        "ok": True,
        "adapter": effective_adapter,
        "model": getattr(adapter, "default_model", None),
        "items_at_start": result.items_at_start,
        "items_audited": result.items_audited,
        "items_cleared": result.items_cleared,
        "items_still_stale": result.items_still_stale,
        "items_errored": result.items_errored,
        "verdicts": result.verdicts,
        "errors": result.errors,
    }
    print(json.dumps(payload, indent=2))
    return 0


def _redefine_stale(
    config: AppConfig,
    *,
    max_items: int,
    adapter_name: str | None,
    model_override: str | None,
    min_confidence: float,
    skip_intent_backfill: bool = False,
) -> int:
    import logging
    from mahalath.adapters import make_adapter
    from mahalath.adapters.base import AdapterError
    from mahalath.db import close_all, get_database
    from mahalath.staleness import redefine_pending_stale

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    effective_adapter = adapter_name or config.runtime.model_adapter
    try:
        adapter = make_adapter(effective_adapter, config)
    except AdapterError as exc:
        print(f"mahalath: {exc}", file=sys.stderr)
        return 12

    if model_override:
        adapter.default_model = model_override

    try:
        db = get_database(config)
        result = redefine_pending_stale(
            config, db, adapter,
            max_items=max_items, min_confidence=min_confidence,
            intent_backfill=not skip_intent_backfill,
        )
        if result.items_redefined > 0:
            from mahalath.glossary import refresh_glossary
            refresh_glossary(config, db)
    finally:
        close_all()

    payload = {
        "ok": True,
        "adapter": effective_adapter,
        "model": getattr(adapter, "default_model", None),
        "items_at_start": result.items_at_start,
        "items_redefined": result.items_redefined,
        "items_skipped": result.items_skipped,
        "items_errored": result.items_errored,
        "verdicts": result.verdicts,
        "errors": result.errors,
        "intent_backfill": result.intent_backfill,
    }
    print(json.dumps(payload, indent=2))
    return 0


def _frontier_review(
    config: AppConfig,
    *,
    max_items: int,
    adapter_name: str,
    model_override: str | None,
) -> int:
    import logging
    from mahalath.adapters import make_adapter
    from mahalath.adapters.base import AdapterError
    from mahalath.db import close_all, ensure_indexes, get_database
    from mahalath.frontier import frontier_review

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    try:
        adapter = make_adapter(adapter_name, config)
    except AdapterError as exc:
        print(f"mahalath: {exc}", file=sys.stderr)
        return 12

    if model_override:
        adapter.default_model = model_override

    try:
        db = get_database(config)
        ensure_indexes(db)
        result = frontier_review(config, db, adapter, max_items=max_items)
    finally:
        close_all()

    payload = {
        "ok": True,
        "adapter": adapter_name,
        "model": getattr(adapter, "default_model", None),
        "items_in_queue_at_start": result.items_in_queue_at_start,
        "items_reviewed": result.items_reviewed,
        "items_accepted": result.items_accepted,
        "items_rejected": result.items_rejected,
        "items_escalated": result.items_escalated,
        "items_errored": result.items_errored,
        "verdicts": result.verdicts,
        "errors": result.errors,
    }
    print(json.dumps(payload, indent=2))
    return 0


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


def _retrieve(
    config: AppConfig,
    *,
    terms: list[str],
    language: str,
    limit: int,
    branch: str | None,
    context: str | None,
    status: str | None,
    min_confidence: float | None,
    intent: str | None,
    matches_only: bool,
    budget: int,
    out_format: str,
) -> int:
    from mahalath.db import close_all, ensure_indexes, get_database
    from mahalath.retrieval import Filters, build_bundle, search_terms

    try:
        db = get_database(config)
        ensure_indexes(db)  # makes sure the $text index exists
    except Exception as exc:  # pragma: no cover
        print(f"mahalath: MongoDB unreachable: {exc}", file=sys.stderr)
        return 4

    try:
        filters = Filters(
        language=language,
            branch=branch,
            context_name=context,
            status=status,
            min_confidence=min_confidence,
            intent_tag=intent,
        )
        if matches_only:
            matches = search_terms(db, terms, filters=filters, limit=limit)
            print(json.dumps({
                "ok": True,
                "terms": terms,
                "match_count": len(matches),
                "matches": [m.to_dict() for m in matches],
            }, indent=2))
            return 0

        bundle = build_bundle(
            db, terms, token_budget=budget, filters=filters,
            limit_per_term=limit,
        )
        if out_format == "text":
            print(bundle.as_text)
        else:
            print(json.dumps(
                {"ok": True, "terms": terms, "bundle": bundle.to_dict()},
                indent=2,
            ))
        return 0
    finally:
        close_all()


def _propose_term(
    config: AppConfig,
    *,
    term: str,
    context: str | None,
    near: str | None,
    dry_run: bool,
    language: str = "en",
) -> int:
    from mahalath.db import close_all, ensure_indexes, get_database
    from mahalath.retrieval import propose_term

    try:
        db = get_database(config)
        ensure_indexes(db)  # the $text index backs the coverage check
    except Exception as exc:  # pragma: no cover
        print(f"mahalath: MongoDB unreachable: {exc}", file=sys.stderr)
        return 4

    try:
        template = propose_term(
            db, term, context=context, near=near, enqueue=not dry_run,
            language=language,
        )
        print(json.dumps({"ok": True, **template.to_dict()}, indent=2))
        return 0
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
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
