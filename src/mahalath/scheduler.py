"""APScheduler harness for autonomous Mahalath operation.

Two jobs:

  poll-input   Every `ingestion.poll_interval_seconds`, run the
               process-input pipeline. Idempotent: docs whose
               `processed_at` is set are skipped in <1 ms each.

  rem          On `rem.cron` (default `0 2 * * *`), run the REM
               nightly job. First slice: count the undecided queue
               and log. Re-running debate on pending items is a
               separate future slice (the audit trail makes that
               straightforward; this just opens the seam).

The CLI exposes a single `mahalath run` command. Default behaviour is
to block in the foreground until Ctrl-C; `--once` fires both jobs
immediately and exits (useful when an external scheduler — systemd
timer, cron, kubernetes — owns the cadence).

Job functions are injectable so tests can inspect calls without
actually polling MongoDB or shelling out to Ollama. Defaults call the
real CLI code paths.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from mahalath.config import AppConfig

log = logging.getLogger("mahalath.scheduler")

JobFn = Callable[[AppConfig], None]


def run_scheduler(
    config: AppConfig,
    *,
    once: bool = False,
    poll_seconds: int | None = None,
    rem_cron: str | None = None,
    process_input_fn: JobFn | None = None,
    rem_fn: JobFn | None = None,
) -> int:
    """Run the polling + REM scheduler.

    Returns an exit code (0 on clean shutdown). When `once` is True the
    function returns as soon as both jobs have fired; otherwise it
    blocks until the BlockingScheduler shuts down (SIGINT or explicit
    shutdown).
    """
    poll_interval = poll_seconds or config.ingestion.poll_interval_seconds
    cron_expr = rem_cron or config.rem.cron
    rem_enabled = config.rem.enabled

    process_input_fn = process_input_fn or _default_process_input
    rem_fn = rem_fn or _default_rem_job

    if once:
        log.info(
            "scheduler: --once mode (poll interval %ds, rem cron %s, "
            "rem enabled=%s)",
            poll_interval, cron_expr, rem_enabled,
        )
        process_input_fn(config)
        if rem_enabled:
            rem_fn(config)
        return 0

    scheduler = BlockingScheduler()
    scheduler.add_job(
        process_input_fn,
        IntervalTrigger(seconds=poll_interval),
        args=[config],
        id="poll-input",
        name="process-input poll",
        next_run_time=datetime.now(timezone.utc),
    )
    if rem_enabled:
        scheduler.add_job(
            rem_fn,
            CronTrigger.from_crontab(cron_expr),
            args=[config],
            id="rem-job",
            name="REM nightly re-review",
        )

    log.info(
        "scheduler: starting; poll every %ds; rem cron %s",
        poll_interval, cron_expr if rem_enabled else "(disabled)",
    )
    log.info("scheduler: Ctrl-C to stop")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler: interrupted, shutting down")
        scheduler.shutdown(wait=False)
    return 0


# --- Default job functions -------------------------------------------------


def _default_process_input(config: AppConfig) -> None:
    """Wraps the CLI's process-input handler so the scheduler picks up
    every behavioural change to that handler for free."""
    from mahalath.cli import _process_input

    log.info("scheduler: process-input poll starting")
    rc = _process_input(
        config,
        max_terms_per_doc=1,
        skip_hierarchy_review=False,
        consensus_passes_override=None,
    )
    log.info("scheduler: process-input poll completed (exit %d)", rc)


def _default_rem_job(config: AppConfig) -> None:
    """REM job: re-debate eligible undecided items via `rem_review`.

    Deliberately does NOT call `db.close_all()`: a long-running
    scheduler owns the connection pool lifecycle, and individual jobs
    closing the shared client would break in-flight work in other jobs.
    """
    from mahalath.adapters import make_adapter
    from mahalath.db import get_database
    from mahalath.rem import rem_review

    log.info("scheduler: REM job starting")
    db = get_database(config)
    adapter = make_adapter(config.runtime.model_adapter, config)
    result = rem_review(config, db, adapter)
    log.info(
        "scheduler: REM completed — pending=%d reviewed=%d accepted=%d "
        "still_undecided=%d skipped=%d errored=%d",
        result.items_pending_at_start,
        result.items_reviewed,
        result.items_accepted,
        result.items_still_undecided,
        result.items_skipped_max_escalation,
        result.items_errored,
    )
    if result.accepted_labels:
        log.info("scheduler: REM promoted: %s", result.accepted_labels)
    if result.errors:
        for err in result.errors[:5]:
            log.warning("scheduler: REM error: %s", err)

    # Stale-audit pass: ask the model whether stale-flagged entries are
    # still consistent with their current upstream state. Cheap pass uses
    # the same gemma4:e2b runtime adapter; high-stakes runs can call
    # `mahalath audit-stale --adapter claude_api` manually.
    from mahalath.staleness import audit_pending_stale, redefine_pending_stale
    audit = audit_pending_stale(config, db, adapter, max_items=10)
    log.info(
        "scheduler: REM stale-audit — at_start=%d audited=%d cleared=%d "
        "still_stale=%d errored=%d",
        audit.items_at_start, audit.items_audited, audit.items_cleared,
        audit.items_still_stale, audit.items_errored,
    )

    # Redefine pass: items the audit declared inconsistent get a fresh
    # definition written against current upstream state.
    redef = redefine_pending_stale(config, db, adapter, max_items=10)
    log.info(
        "scheduler: REM stale-redefine — at_start=%d redefined=%d "
        "skipped=%d errored=%d",
        redef.items_at_start, redef.items_redefined,
        redef.items_skipped, redef.items_errored,
    )

    if (
        result.items_accepted > 0
        or audit.items_cleared > 0
        or redef.items_redefined > 0
    ):
        from mahalath.glossary import refresh_glossary
        exports = refresh_glossary(config, db)
        log.info(
            "scheduler: REM refreshed glossary (md=%d entries, json=%d entries)",
            exports["md"].entry_count, exports["json"].entry_count,
        )
