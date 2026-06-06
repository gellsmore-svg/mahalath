"""Smoke tests for the config loader.

These exercise the no-config-file default path and the example-config
fallback path, without requiring MongoDB or any LLM runtime.
"""

from __future__ import annotations

from pathlib import Path

from mahalath.config import (
    AppConfig,
    AgentRolesConfig,
    load_config,
)


def test_load_config_returns_defaults_when_no_yaml_present(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = load_config()
    assert isinstance(config, AppConfig)
    assert config.mongo.database == "mahalath_dev"
    assert config.runtime.max_iterations_per_term == 50
    assert config.runtime.confidence_threshold == 8.0
    assert config.runtime.confidence_scale_max == 10.0


def test_default_agent_roles_match_requirements() -> None:
    roles = AgentRolesConfig()
    # PrecisionCritic and SynthesisExplorer default on; Moderator default
    # off, per requirements §3.2 / DQ-001.
    assert roles.precision_critic.enabled is True
    assert roles.synthesis_explorer.enabled is True
    assert roles.moderator.enabled is False
