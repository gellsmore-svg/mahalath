"""Tests for the HTTP Ollama adapter (offline; network mocked)."""

from __future__ import annotations

import pytest

from mahalath.adapters import ollama_http
from mahalath.adapters.base import AdapterError, EmbeddingNaNError
from mahalath.adapters.ollama_http import OllamaHttpAdapter


def _adapter():
    return OllamaHttpAdapter(base_url="http://h:11434/", default_model="m", embedding_model="e")


def test_post_json_normalizes_malformed_success_response(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"not-json"

    monkeypatch.setattr(ollama_http.urllib.request, "urlopen", lambda *_a, **_k: Response())

    with pytest.raises(AdapterError, match="malformed JSON"):
        ollama_http._post_json("http://h/api/generate", {}, timeout=1)


def test_post_json_normalizes_invalid_utf8(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"\xff"

    monkeypatch.setattr(ollama_http.urllib.request, "urlopen", lambda *_a, **_k: Response())

    with pytest.raises(AdapterError, match="malformed JSON"):
        ollama_http._post_json("http://h/api/embed", {}, timeout=1)


def test_generate_parses_response_and_passes_format(monkeypatch):
    seen = {}

    def fake_post(url, payload, *, timeout):
        seen["url"], seen["payload"] = url, payload
        return {"response": "  hello  ", "eval_count": 3}

    monkeypatch.setattr(ollama_http, "_post_json", fake_post)
    resp = _adapter().generate("hi", want_json=True)
    assert resp.text == "hello" and resp.model == "m"
    assert seen["url"] == "http://h:11434/api/generate"
    assert seen["payload"]["format"] == "json" and seen["payload"]["stream"] is False


def test_generate_connection_error_raises_adaptererror(monkeypatch):
    import urllib.error

    def boom(*a, **k):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(ollama_http, "_post_json", boom)
    with pytest.raises(AdapterError):
        _adapter().generate("hi")


def test_embed_accepts_both_shapes(monkeypatch):
    monkeypatch.setattr(ollama_http, "_post_json", lambda *a, **k: {"embeddings": [[0.1, 0.2, 0.3]]})
    e = _adapter().embed("x")
    assert e.vector == [0.1, 0.2, 0.3] and e.dim == 3

    monkeypatch.setattr(ollama_http, "_post_json", lambda *a, **k: {"embedding": [1.0, 2.0]})
    assert _adapter().embed("x").dim == 2


def test_embed_nonfinite_raises_nan(monkeypatch):
    monkeypatch.setattr(ollama_http, "_post_json", lambda *a, **k: {"embedding": [float("nan")]})
    with pytest.raises(EmbeddingNaNError):
        _adapter().embed("x")


def test_factory_builds_ollama_http():
    from mahalath.adapters.factory import make_adapter
    from mahalath.config import AppConfig

    adapter = make_adapter("ollama_http", AppConfig())
    assert adapter.name == "ollama_http"
