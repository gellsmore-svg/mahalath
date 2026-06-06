"""Adapter package tests.

Mock adapter is exercised here. The ollama_cli adapter is smoked
manually against a live Ollama process; it has no unit tests because
the value is in the live integration, not in mocking subprocess.run.
"""

from __future__ import annotations

import pytest

from mahalath.adapters import (
    Adapter,
    AdapterError,
    AdapterResponse,
    MockAdapter,
    make_adapter,
)
from mahalath.config import AppConfig


def test_mock_adapter_implements_protocol() -> None:
    adapter = MockAdapter()
    assert isinstance(adapter, Adapter)


def test_mock_adapter_default_response() -> None:
    adapter = MockAdapter(default_response="canned")
    resp = adapter.generate("anything")
    assert isinstance(resp, AdapterResponse)
    assert resp.text == "canned"
    assert resp.model == "mock-model"
    assert resp.duration_ms == 0


def test_mock_adapter_substring_match() -> None:
    adapter = MockAdapter(
        responses={
            "precision_critic": '{"verdict": "needs work", "confidence": 6.0}',
            "synthesis_explorer": '{"verdict": "ok", "confidence": 8.5}',
        }
    )
    a = adapter.generate("You are the precision_critic. ...")
    b = adapter.generate("You are the synthesis_explorer. ...")
    assert "needs work" in a.text
    assert "ok" in b.text


def test_mock_adapter_records_calls() -> None:
    adapter = MockAdapter()
    adapter.generate("p1", model="gemma3:1b", want_json=True)
    adapter.generate("p2")
    assert len(adapter.calls) == 2
    assert adapter.calls[0]["model"] == "gemma3:1b"
    assert adapter.calls[0]["want_json"] is True
    assert adapter.calls[1]["prompt"] == "p2"


def test_mock_adapter_uses_passed_model_name() -> None:
    adapter = MockAdapter()
    resp = adapter.generate("hi", model="qwen3.5:35b")
    assert resp.model == "qwen3.5:35b"


def test_factory_rejects_unknown_adapter() -> None:
    config = AppConfig()
    with pytest.raises(AdapterError):
        make_adapter("nonexistent", config)


def test_factory_creates_mock_with_config_model() -> None:
    config = AppConfig()
    adapter = make_adapter("mock", config)
    assert isinstance(adapter, MockAdapter)
    assert adapter.default_model == config.runtime.model
