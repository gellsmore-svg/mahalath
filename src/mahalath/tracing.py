"""Optional Galeed trace emission — witness the ontology pipeline on the family spine.

When ``galeed.enabled`` is on in config, Mahalath testifies its pipeline
lifecycle onto the shared trace stream: documents ingested, debates completed,
proposals decided. Event types are Mahalath-domain extensions of Galeed's open
vocabulary (``document.ingested``, ``debate.completed``, ``proposal.accepted``,
``proposal.rejected``, ``proposal.rolled_back``); the generic ``job.*`` set
stays with infrastructure (Hoglah).

Strictly best-effort and optional: ``galeed`` is imported lazily (install with
the ``galeed`` extra), and every failure is swallowed — tracing must never
affect the pipeline. Events persist to ``galeed.uri`` / ``galeed.database``,
which should point at the database the family trace API (Tirzah ``/api/trace``,
Mizpah) reads.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger("mahalath")

DOCUMENT_INGESTED = "document.ingested"
DEBATE_COMPLETED = "debate.completed"
PROPOSAL_ACCEPTED = "proposal.accepted"
PROPOSAL_REJECTED = "proposal.rejected"
PROPOSAL_ROLLED_BACK = "proposal.rolled_back"


class Witness:
    """Best-effort emitter of pipeline lifecycle events onto the Galeed spine."""

    def __init__(self, config: Any = None, db: Any = None) -> None:
        galeed_cfg = getattr(config, "galeed", None)
        self._enabled = bool(getattr(galeed_cfg, "enabled", False))
        self._cfg = galeed_cfg
        self._db = db
        self._db_resolved = db is not None
        self._db_lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _database(self) -> Any:
        # Locked, resolved-flag-last: concurrent first emissions from different
        # threads must never observe a half-initialised handle (a None here
        # silently drops events).
        with self._db_lock:
            if self._db_resolved:
                return self._db
            try:
                from pymongo import MongoClient

                client = MongoClient(self._cfg.uri, serverSelectionTimeoutMS=2000)
                self._db = client[self._cfg.database]
            except Exception:
                logger.debug("galeed trace db unavailable; emitting bus-only", exc_info=True)
                self._db = None
            self._db_resolved = True
            return self._db

    def emit(
        self,
        type: str,
        *,
        trace_id: str,
        status: str = "completed",
        summary: str = "",
        session_id: str = "mahalath",
        **metadata: Any,
    ) -> None:
        """Emit one lifecycle event; swallows every failure by design."""
        if not self._enabled:
            return
        try:
            from galeed import Tracer
        except ImportError:
            self._enabled = False
            logger.debug("galeed not installed; pipeline tracing disabled")
            return
        try:
            tracer = Tracer(
                trace_id=trace_id,
                session_id=session_id,
                db=self._database(),
                source="mahalath",
            )
            tracer.emit(type, status=status, summary=summary, **metadata)
        except Exception:
            logger.debug("galeed emit failed (ignored)", exc_info=True)


_witness: Witness | None = None


def get_witness() -> Witness:
    """Process-wide witness, built lazily from load_config().

    A singleton so call sites (proposals, debate, ingestion) don't need config
    threaded through their signatures. Tests replace it via set_witness().
    """
    global _witness
    if _witness is None:
        try:
            from mahalath.config import load_config

            _witness = Witness(load_config())
        except Exception:
            _witness = Witness()
    return _witness


def set_witness(witness: Witness | None) -> None:
    """Override (or reset with None) the process-wide witness — for tests."""
    global _witness
    _witness = witness
