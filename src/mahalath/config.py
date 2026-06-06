"""Mahalath configuration loader.

Loads `config.yaml` (operator's local) when present, falling back to
`config.example.yaml` (committed) so a fresh checkout is runnable
without any operator-side configuration.

Schema deliberately mirrors `~/domains/Mnemosyne/src/mnemosyne/config.py`
in structure (MongoConfig, PathConfig, RuntimeConfig nested under
AppConfig) where the concerns overlap. Mahalath-specific fields
(agent role enablement, debate iteration cap, confidence threshold,
REM cron schedule) live under their own sub-models.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class MongoConfig(BaseModel):
    uri: str = "mongodb://localhost:27017"
    database: str = "mahalath_dev"


class PathConfig(BaseModel):
    input: Path = Path("input")
    processed: Path = Path("processed")
    ontology: Path = Path("ontology")
    logs: Path = Path("logs")
    undecided: Path = Path("undecided")


class AgentRoleConfig(BaseModel):
    enabled: bool = True
    system_prompt_path: str | None = None


class AgentRolesConfig(BaseModel):
    precision_critic: AgentRoleConfig = Field(default_factory=AgentRoleConfig)
    synthesis_explorer: AgentRoleConfig = Field(default_factory=AgentRoleConfig)
    moderator: AgentRoleConfig = Field(
        default_factory=lambda: AgentRoleConfig(enabled=False)
    )


class RuntimeConfig(BaseModel):
    model_adapter: str = "ollama_cli"
    model: str = "gemma4:e2b"
    agents: AgentRolesConfig = Field(default_factory=AgentRolesConfig)
    max_iterations_per_term: int = 50
    confidence_threshold: float = 8.0
    confidence_scale_max: float = 10.0
    ollama_executable: Path = Path(
        "/mnt/c/Users/cello/AppData/Local/Programs/Ollama/ollama.exe"
    )
    ollama_base_url: str = "http://localhost:11434"
    ollama_timeout_seconds: int = 180


class IngestionConfig(BaseModel):
    poll_interval_seconds: int = 60
    duplicate_rejection: str = "sha256"


class RemConfig(BaseModel):
    enabled: bool = True
    cron: str = "0 2 * * *"


class AppConfig(BaseModel):
    mongo: MongoConfig = Field(default_factory=MongoConfig)
    paths: PathConfig = Field(default_factory=PathConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    ingestion: IngestionConfig = Field(default_factory=IngestionConfig)
    rem: RemConfig = Field(default_factory=RemConfig)


def load_config(path: Path | str = "config.yaml") -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        example_path = Path("config.example.yaml")
        if example_path.exists():
            config_path = example_path
        else:
            return AppConfig()
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return AppConfig.model_validate(data)
