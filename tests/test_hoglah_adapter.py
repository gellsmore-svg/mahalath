"""Tests for the `hoglah` adapter (route LLM calls via a Hoglah queue daemon).

The "daemon" here is a real in-test Hoglah worker instance using Hoglah's
default StubAdapter, pointed at the same shared db_path + output_dir as the
HoglahAdapter-under-test. That exercises the genuine submit -> daemon executes
-> writes output file -> Mahalath polls path without needing Ollama.
"""

from __future__ import annotations

import json

import pytest

from mahalath.adapters import Adapter, AdapterError, make_adapter
from mahalath.adapters.base import EmbeddingNaNError
from mahalath.adapters.hoglah import HoglahAdapter
from mahalath.config import AppConfig

hoglah = pytest.importorskip("hoglah")


def _adapter(db_path, out_dir, **kw) -> HoglahAdapter:
    return HoglahAdapter(
        db_path=str(db_path),
        output_dir=str(out_dir),
        default_model="stub-model:1b",
        embedding_model="stub-embed",
        poll_interval_seconds=0.05,
        **kw,
    )


def test_hoglah_adapter_implements_protocol(tmp_path) -> None:
    adapter = _adapter(tmp_path / "q.db", tmp_path / "out")
    assert isinstance(adapter, Adapter)
    assert adapter.name == "hoglah"


def test_hoglah_adapter_generate_via_stub_worker(tmp_path) -> None:
    db = tmp_path / "q.db"
    out = tmp_path / "out"
    worker = hoglah.Hoglah(
        config={"db_path": str(db), "output_dir": str(out)}, start_worker=True
    )
    try:
        adapter = _adapter(db, out)
        resp = adapter.generate("hello there", timeout_seconds=5)
        assert "[STUB]" in resp.text
        assert "hello there" in resp.text
        assert resp.model == "stub-model:1b"
        assert resp.raw["via"] == "hoglah"
    finally:
        worker.close()


def test_hoglah_adapter_embed_via_stub_worker(tmp_path) -> None:
    db = tmp_path / "q.db"
    out = tmp_path / "out"
    worker = hoglah.Hoglah(
        config={"db_path": str(db), "output_dir": str(out)}, start_worker=True
    )
    try:
        adapter = _adapter(db, out)
        resp = adapter.embed("some meaning", timeout_seconds=5)
        # StubAdapter.embed returns a deterministic dim-8 finite vector.
        assert len(resp.vector) == 8
        assert resp.dim == 8
        assert all(isinstance(x, float) for x in resp.vector)
    finally:
        worker.close()


def test_hoglah_adapter_times_out_without_worker(tmp_path) -> None:
    """With no daemon writing the output file, the poll path raises a clear
    AdapterError pointing at the missing worker once the deadline passes."""
    out = tmp_path / "out"
    out.mkdir()
    adapter = _adapter(tmp_path / "q.db", out)
    with pytest.raises(AdapterError, match="worker"):
        adapter._await_result("never-written", timeout=0.3)


def test_hoglah_adapter_maps_non_finite_to_nan_error(tmp_path, monkeypatch) -> None:
    """A failed embedding job whose error mentions a non-finite vector is
    surfaced as EmbeddingNaNError so Mahalath's NaN-resilience applies."""
    out = tmp_path / "out"
    out.mkdir()
    adapter = _adapter(tmp_path / "q.db", out)

    job_id = "emb-nan-1"
    (out / f"{job_id}.json").write_text(
        json.dumps(
            {
                "job_id": job_id,
                "status": "failed",
                "error": "bge-m3 produced a non-finite embedding (model numerical instability)",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(adapter._client, "submit_embedding", lambda *a, **k: job_id)

    with pytest.raises(EmbeddingNaNError):
        adapter.embed("pathological input", timeout_seconds=5)


def test_factory_creates_hoglah_adapter(tmp_path) -> None:
    cfg = AppConfig()
    cfg.runtime.model_adapter = "hoglah"
    cfg.runtime.hoglah.db_path = str(tmp_path / "q.db")
    cfg.runtime.hoglah.output_dir = str(tmp_path / "out")
    adapter = make_adapter("hoglah", cfg)
    assert isinstance(adapter, HoglahAdapter)
    assert adapter.default_model == cfg.runtime.model
    assert adapter.embedding_model == cfg.runtime.embedding_model


# -- delivery="callback" (M2) ------------------------------------------------


def _callback_adapter(db_path, out_dir, **kw) -> HoglahAdapter:
    return _adapter(
        db_path, out_dir, delivery="callback", callback_host="127.0.0.1", callback_port=0, **kw
    )


def test_hoglah_adapter_callback_generate_via_stub_worker(tmp_path) -> None:
    """Callback mode: the stub worker POSTs the result to Mahalath's receiver at
    the URL Mahalath supplied per job, and generate() unblocks with it."""
    db = tmp_path / "q.db"
    out = tmp_path / "out"
    worker = hoglah.Hoglah(
        config={"db_path": str(db), "output_dir": str(out)}, start_worker=True
    )
    adapter = _callback_adapter(db, out)
    try:
        # Mahalath owns the URL; it is not baked into Hoglah.
        assert adapter._callback_url and adapter._callback_url.startswith("http://127.0.0.1:")
        resp = adapter.generate("hello callback", timeout_seconds=5)
        assert "[STUB]" in resp.text
        assert "hello callback" in resp.text
        emb = adapter.embed("meaning", timeout_seconds=5)
        assert len(emb.vector) == 8
    finally:
        adapter.close()
        worker.close()


def test_hoglah_adapter_callback_falls_back_to_output_file(tmp_path, monkeypatch) -> None:
    """If the push never arrives but the daemon wrote the output file, the
    callback waiter falls back to the file rather than failing."""
    out = tmp_path / "out"
    out.mkdir()
    adapter = _callback_adapter(tmp_path / "q.db", out)
    try:
        job_id = "cb-fallback-1"
        (out / f"{job_id}.json").write_text(
            json.dumps(
                {"job_id": job_id, "status": "completed", "output": "from-file", "model": "m"}
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(adapter._client, "submit", lambda **k: job_id)
        # Short timeout: no callback will arrive, so it must use the file.
        resp = adapter.generate("hi", timeout_seconds=1)
        assert resp.text == "from-file"
    finally:
        adapter.close()


def test_hoglah_callback_port_is_configurable(tmp_path) -> None:
    """callback_port=0 yields an OS-assigned port; a fixed port is honoured and
    reflected in the advertised URL."""
    # ephemeral
    a0 = _adapter(tmp_path / "a.db", tmp_path / "o", delivery="callback",
                  callback_host="127.0.0.1", callback_port=0)
    try:
        assert a0._receiver.port > 0
        assert f":{a0._receiver.port}" in a0._callback_url
    finally:
        a0.close()

    # explicit port (best-effort: skip if the OS won't give it to us)
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    free_port = s.getsockname()[1]
    s.close()
    a1 = _adapter(tmp_path / "b.db", tmp_path / "o", delivery="callback",
                  callback_host="127.0.0.1", callback_port=free_port)
    try:
        assert a1._receiver.port == free_port
        assert a1._callback_url == f"http://127.0.0.1:{free_port}/hoglah/callback"
    finally:
        a1.close()


def test_hoglah_adapter_rejects_unknown_delivery(tmp_path) -> None:
    with pytest.raises(AdapterError, match="delivery"):
        _adapter(tmp_path / "q.db", tmp_path / "o", delivery="telepathy")


# --------------------------------------------------------------------------- #
# Messaging transport (kafka / rabbitmq / redis) — routed through Hoglah's
# MessagingSubmitter. Exercised broker-free with a fake submitter transport.
# --------------------------------------------------------------------------- #

pytest.importorskip("hoglah.messaging_submitter")


class _FakeTransport:
    def __init__(self) -> None:
        self.published: list[tuple[bytes, str]] = []
        self.kinds: list[str] = []

    def reply_destination(self):
        return "hoglah-results"

    def publish_request(self, body, *, correlation_id):
        self.published.append((body, correlation_id))

    def await_result(self, correlation_id, timeout):
        kind = json.loads(self.published[-1][0]).get("kind")
        self.kinds.append(kind)
        base = {"correlation_id": correlation_id, "status": "completed", "job_id": "jX", "model": "stub-model:1b"}
        if kind == "embed":
            return {**base, "embedding": [0.1, 0.2, 0.3], "embedding_dim": 3}
        return {**base, "output": "[STUB] queued answer"}

    def close(self):
        pass


@pytest.fixture
def fake_transport(monkeypatch):
    fake = _FakeTransport()
    monkeypatch.setattr(
        "hoglah.messaging_submitter.make_submitter_transport", lambda *a, **k: fake
    )
    return fake


def _messaging_adapter(transport):
    return HoglahAdapter(
        db_path="unused", output_dir="unused",
        default_model="stub-model:1b", embedding_model="stub-embed",
        transport=transport,
    )


def test_hoglah_messaging_routes_generate(fake_transport) -> None:
    adapter = _messaging_adapter("redis")
    assert adapter._client is None and adapter._submitter is not None  # messaging, not store
    resp = adapter.generate("hello there", timeout_seconds=5)
    assert resp.text == "[STUB] queued answer"
    assert resp.raw["via"] == "hoglah" and resp.raw["transport"] == "redis"
    assert fake_transport.kinds == ["generate"]
    adapter.close()


def test_hoglah_messaging_routes_embed(fake_transport) -> None:
    adapter = _messaging_adapter("kafka")
    emb = adapter.embed("vorton", timeout_seconds=5)
    assert emb.vector == [0.1, 0.2, 0.3]
    assert emb.dim == 3
    assert fake_transport.kinds == ["embed"]
    adapter.close()
