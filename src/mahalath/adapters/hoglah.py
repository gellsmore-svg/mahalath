"""Adapter that routes generation + embedding through a Hoglah queue daemon.

Instead of calling Ollama directly (like `ollama_cli`), this adapter submits
each call as a job into a shared Hoglah SQLite queue and waits for the result.
A SEPARATE `hoglah run` worker daemon — pointed at the same `db_path` and
`output_dir` — actually executes the job against Ollama. This buys durability,
serialized GPU access (Hoglah concurrency defaults to 1), and per-job timeout
enforcement for a walk-away run, without changing Mahalath's synchronous
`generate()` / `embed()` call sites.

Why a daemon and not an in-process worker: the whole point is to decouple
submission from execution so a single queue serialises every model call across
the pipeline. Mahalath constructs the Hoglah client as a pure submitter
(`start_worker=False`, ADR-016 over there) so it never executes jobs or
re-queues the daemon's in-flight work.

Result delivery (config `runtime.hoglah.delivery`):
- "poll" (this module): submit -> poll `output_dir/<job_id>.json` until it
  appears (Hoglah ADR-014 writes it atomically).
- "callback": handled by HoglahCallbackAdapter (M2), which has Hoglah POST the
  result to a local receiver and falls back to polling.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from mahalath.adapters.base import (
    AdapterError,
    AdapterResponse,
    EmbeddingNaNError,
    EmbeddingResponse,
)


class _CallbackReceiver:
    """Tiny generic HTTP receiver for Hoglah result callbacks (delivery="callback").

    Hoglah POSTs the terminal JobResult JSON to the URL Mahalath supplied at
    submit time (per-job `callback_url` — nothing about Mahalath is baked into
    Hoglah). This server matches each POST to a waiting `generate()`/`embed()`
    call by `job_id`. A result that arrives before its waiter has registered
    (the submit/register race) is held until the waiter asks for it.
    """

    def __init__(self, host: str, port: int, path: str = "/hoglah/callback") -> None:
        self.path = path
        self._lock = threading.Lock()
        self._events: dict[str, threading.Event] = {}
        self._results: dict[str, dict[str, Any]] = {}

        receiver = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
                if self.path != receiver.path:
                    self.send_response(404)
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    doc = json.loads(raw.decode("utf-8"))
                    job_id = doc["job_id"]
                except (ValueError, KeyError):
                    self.send_response(400)
                    self.end_headers()
                    return
                receiver._deliver(job_id, doc)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args: Any) -> None:  # silence default stderr logging
                pass

        # port=0 -> OS assigns an ephemeral port; read it back for the URL.
        self._server = ThreadingHTTPServer((host, port), _Handler)
        self.host = host
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="mahalath-hoglah-cb"
        )
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}{self.path}"

    def _deliver(self, job_id: str, doc: dict[str, Any]) -> None:
        with self._lock:
            self._results[job_id] = doc
            ev = self._events.get(job_id)
        if ev is not None:
            ev.set()

    def wait_for(self, job_id: str, timeout: float) -> dict[str, Any] | None:
        """Block until the callback for `job_id` arrives or `timeout` elapses.
        Returns the result doc, or None on timeout."""
        with self._lock:
            if job_id in self._results:
                return self._results.pop(job_id)
            ev = self._events.setdefault(job_id, threading.Event())
        ev.wait(timeout)
        with self._lock:
            self._events.pop(job_id, None)
            return self._results.pop(job_id, None)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class HoglahAdapter:
    """Submit-and-poll adapter backed by a Hoglah queue daemon."""

    name: str = "hoglah"

    def __init__(
        self,
        *,
        db_path: str,
        output_dir: str,
        default_model: str,
        embedding_model: str,
        default_timeout_seconds: int = 600,
        poll_interval_seconds: float = 0.5,
        delivery: str = "poll",
        callback_host: str = "127.0.0.1",
        callback_port: int = 0,
        transport: str = "store",
        kafka_bootstrap_servers: str = "localhost:9092",
        kafka_input_topic: str = "hoglah-jobs",
        kafka_results_topic: str = "hoglah-results",
        rabbitmq_url: str = "amqp://guest:guest@localhost:5672/",
        rabbitmq_input_queue: str = "hoglah-jobs",
        redis_url: str = "redis://localhost:6379/0",
        redis_input_stream: str = "hoglah-jobs",
        redis_results_stream: str = "hoglah-results",
    ) -> None:
        self.default_model = default_model
        self.embedding_model = embedding_model
        self.default_timeout_seconds = default_timeout_seconds
        self.transport = transport
        self._client: Any | None = None
        self._submitter: Any | None = None
        self._receiver: _CallbackReceiver | None = None
        self._callback_url: str | None = None

        if transport != "store":
            # Messaging path: publish a job-request over a broker and await the
            # result over the same broker. No SQLite client, no callback receiver;
            # the matching `hoglah {kafka,rabbitmq,redis}-bridge` worker executes it.
            try:
                from hoglah.messaging_submitter import (
                    MessagingSubmitter,
                    make_submitter_transport,
                )
            except ImportError as exc:  # pragma: no cover - exercised via factory
                raise AdapterError(
                    "The 'hoglah' adapter with a messaging transport requires the hoglah "
                    "package and the broker client. Install with: "
                    "pip install 'mahalath[hoglah-kafka]' (or hoglah-rabbitmq / hoglah-redis)."
                ) from exc
            self._submitter = MessagingSubmitter(
                make_submitter_transport(
                    transport,
                    kafka_bootstrap_servers=kafka_bootstrap_servers,
                    kafka_input_topic=kafka_input_topic,
                    kafka_results_topic=kafka_results_topic,
                    rabbitmq_url=rabbitmq_url,
                    rabbitmq_input_queue=rabbitmq_input_queue,
                    redis_url=redis_url,
                    redis_input_stream=redis_input_stream,
                    redis_results_stream=redis_results_stream,
                )
            )
            return

        # Store path (default): the shared SQLite queue + poll/callback delivery.
        # Lazy import so hoglah stays an optional dependency: only the operator
        # who selects the "hoglah" adapter needs it installed.
        try:
            from hoglah import Hoglah
        except ImportError as exc:  # pragma: no cover - exercised via factory
            raise AdapterError(
                "The 'hoglah' adapter requires the hoglah package. "
                "Install it with: pip install 'mahalath[hoglah]' (or pip install hoglah)."
            ) from exc

        if delivery not in ("poll", "callback"):
            raise AdapterError(
                f"Unknown hoglah delivery mode {delivery!r}; expected 'poll' or 'callback'."
            )

        self.db_path = str(Path(db_path).expanduser())
        self.output_dir = Path(output_dir).expanduser()
        self.poll_interval_seconds = poll_interval_seconds
        self.delivery = delivery

        # Callback mode: stand up a local HTTP receiver and advertise its URL.
        # Mahalath OWNS this URL and passes it to Hoglah per job — Hoglah just
        # POSTs to whatever it's given. The output folder remains a fallback.
        if delivery == "callback":
            self._receiver = _CallbackReceiver(callback_host, callback_port)
            self._callback_url = self._receiver.url

        # Pure submitter: no worker, no interrupted-job recovery (ADR-016). The
        # client writes into the shared queue; the daemon executes. We pass
        # output_dir so a daemon started FROM this same config knows where to
        # write — but the daemon is the one that actually writes results.
        self._client = Hoglah(
            config={"db_path": self.db_path, "output_dir": str(self.output_dir)},
            start_worker=False,
        )

    # -- submission helpers -------------------------------------------------

    def _read_output_file(self, job_id: str) -> dict[str, Any] | None:
        """Return the parsed `output_dir/<job_id>.json` if present and complete.

        Hoglah writes it atomically (temp + os.replace), so a partial read is
        not a concern; a transient decode/IO error just means "not ready yet"
        and yields None so the caller can retry.
        """
        dest = self.output_dir / f"{job_id}.json"
        if not dest.exists():
            return None
        try:
            return json.loads(dest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _await_result(self, job_id: str, timeout: float) -> dict[str, Any]:
        """Wait for a job's terminal result, or raise AdapterError on timeout.

        callback mode: block on the HTTP receiver for this job_id; if the push
        never lands (e.g. the daemon couldn't reach us), fall back to reading
        the output file the daemon also wrote. poll mode: poll the output file.
        """
        if self._receiver is not None:
            doc = self._receiver.wait_for(job_id, timeout)
            if doc is not None:
                return doc
            doc = self._read_output_file(job_id)  # push missed; try the file
            if doc is not None:
                return doc
            raise AdapterError(
                f"Hoglah job {job_id}: no callback received within {timeout:.0f}s "
                f"and no output file in {self.output_dir}. Is the 'hoglah run' "
                f"worker running, and can it reach the callback URL "
                f"{self._callback_url}?"
            )

        deadline = time.monotonic() + timeout
        while True:
            doc = self._read_output_file(job_id)
            if doc is not None:
                return doc
            if time.monotonic() > deadline:
                raise AdapterError(
                    f"Hoglah job {job_id} produced no result within {timeout:.0f}s "
                    f"(looked in {self.output_dir}). Is the 'hoglah run' worker "
                    f"daemon running and pointed at the same db_path/output_dir?"
                )
            time.sleep(self.poll_interval_seconds)

    def _run(
        self,
        kind: str,
        *,
        prompt: str,
        model: str,
        timeout: int,
        want_json: bool = False,
    ) -> dict[str, Any]:
        """Submit a job and return its terminal result dict, via the configured
        transport — a messaging broker (MessagingSubmitter) or the SQLite store
        (submit + poll/callback). The result shape is identical either way."""
        if self._submitter is not None:
            return self._submitter.submit(
                kind=kind,
                prompt=prompt,
                model=model,
                timeout=float(timeout) + 30,  # slack for daemon pickup, as in the store path
                fmt="json" if (want_json and kind == "generate") else None,
                tags=["mahalath"],
                metadata={"source": "mahalath"},
            )
        if kind == "embed":
            job_id = self._client.submit_embedding(
                prompt, model=model, timeout_seconds=int(timeout),
                callback_url=self._callback_url,
            )
        else:
            job_id = self._client.submit(
                prompt=prompt, model=model, timeout_seconds=int(timeout),
                format="json" if want_json else None, callback_url=self._callback_url,
            )
        result = self._await_result(job_id, timeout + 30)
        result.setdefault("job_id", job_id)
        return result

    def close(self) -> None:
        """Shut down the messaging submitter / callback receiver. Safe to call repeatedly."""
        if self._submitter is not None:
            self._submitter.close()
            self._submitter = None
        if self._receiver is not None:
            self._receiver.close()
            self._receiver = None

    @staticmethod
    def _require_completed(result: dict[str, Any], job_id: str) -> None:
        status = result.get("status")
        if status != "completed":
            err = result.get("error") or f"job ended in status {status!r}"
            raise AdapterError(f"Hoglah job {job_id} did not complete: {err}")

    # -- Adapter protocol ---------------------------------------------------

    def generate(
        self,
        prompt: str,
        *,
        model: str | None = None,
        timeout_seconds: int | None = None,
        want_json: bool = False,
    ) -> AdapterResponse:
        model_name = model or self.default_model
        timeout = timeout_seconds or self.default_timeout_seconds

        start = time.monotonic()
        result = self._run("generate", prompt=prompt, model=model_name, timeout=timeout, want_json=want_json)
        job_id = result.get("job_id", "?")
        self._require_completed(result, job_id)

        return AdapterResponse(
            text=result.get("output") or "",
            model=result.get("model") or model_name,
            duration_ms=int((time.monotonic() - start) * 1000),
            raw={"job_id": job_id, "via": "hoglah", "transport": self.transport, "metadata": result.get("metadata", {})},
        )

    def embed(
        self,
        text: str,
        *,
        model: str | None = None,
        timeout_seconds: int | None = None,
    ) -> EmbeddingResponse:
        model_name = model or self.embedding_model
        timeout = timeout_seconds or self.default_timeout_seconds

        start = time.monotonic()
        result = self._run("embed", prompt=text, model=model_name, timeout=timeout)
        job_id = result.get("job_id", "?")

        # Hoglah fails the job (not a bogus vector) when the model emits a
        # non-finite value; surface that as EmbeddingNaNError so Mahalath's
        # existing NaN-resilience (skip/retry) applies rather than aborting.
        if result.get("status") != "completed":
            err = (result.get("error") or "").lower()
            if "non-finite" in err or "nan" in err:
                raise EmbeddingNaNError(
                    f"Hoglah embedding job {job_id} hit a non-finite vector: "
                    f"{result.get('error')}"
                )
            self._require_completed(result, job_id)

        vector = result.get("embedding")
        if not isinstance(vector, list) or not vector:
            raise AdapterError(
                f"Hoglah embedding job {job_id} returned no vector "
                f"(status={result.get('status')!r})."
            )
        floats = [float(x) for x in vector]
        return EmbeddingResponse(
            vector=floats,
            model=result.get("model") or model_name,
            dim=result.get("embedding_dim") or len(floats),
            duration_ms=int((time.monotonic() - start) * 1000),
        )
