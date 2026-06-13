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
    # Per-role model override (DQ-010, operator-ruled 2026-06-12):
    # putting the two debaters on DIFFERENT model families makes
    # min(pc, se) genuinely cross-model. None -> runtime.model.
    model: str | None = None


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
    # Hierarchy review consensus: number of independent review passes
    # required to agree (unanimously) on an action before it is eligible
    # for dispatch. Set to 1 to disable consensus (single-pass behavior).
    hierarchy_consensus_passes: int = 3
    # Intent attribution consensus (I-B, ADR-025): number of independent
    # attribution passes; a tag is stored only if EVERY pass proposes it,
    # intentionality only if every pass agrees on the ordinal, and the
    # stored intent_confidence is the minimum across passes. Set to 1 to
    # disable consensus (NOT recommended for model-sourced tags).
    intent_consensus_passes: int = 3
    # Model roster for consensus passes (hierarchy review + intent
    # attribution): pass i runs on consensus_models[i % len]. Unanimity
    # across model FAMILIES is far stronger evidence than the same
    # model agreeing with itself N times. None -> every pass on
    # runtime.model (pre-S2.46 behaviour).
    consensus_models: list[str] | None = None
    # Per-corpus style overlay: path (relative to project root) of a
    # Markdown file injected into every agent prompt as
    # `## Style guidance for this corpus`. Carries voice notes, domain
    # vocabulary, "term X in this corpus means Y" overrides. None means
    # no overlay; default behaviour matches Stage 1.
    style_overlay_path: str | None = None
    # Adapter used by the /api/chat endpoint. Defaults to ollama_cli
    # so a fresh install works end-to-end without an external API key.
    # Override to "claude_api" (and set ANTHROPIC_API_KEY) for
    # frontier-quality answers if the operator accepts the per-request
    # cost.
    chat_adapter: str = "ollama_cli"
    ollama_executable: Path = Path(
        "/mnt/c/Users/cello/AppData/Local/Programs/Ollama/ollama.exe"
    )
    ollama_base_url: str = "http://localhost:11434"
    # Generous by design: the FIRST call after idle loads a multi-GB
    # model from disk, which takes minutes on laptop hardware (operator
    # direction 2026-06-11: "wait longer for llm calls"). Raised 180→600.
    ollama_timeout_seconds: int = 600
    # Cross-language candidate fingerprinting (M-C, ADR-031-adjacent).
    # The embedding model turns a definition's meaning into a vector so
    # candidate pairs are found by meaning-closeness instead of a model
    # eyeballing a list. MUST be multilingual (de↔en) — bge-m3 covers
    # 100+ languages incl. CJK. Lives behind the adapter's embed().
    embedding_model: str = "bge-m3"
    # None -> use model_adapter for embeddings too.
    embedding_adapter: str | None = None
    # generate-mappings candidate stage: "embedding" (meaning-closeness,
    # needs backfilled vectors), "prompt" (the model picks from a
    # snapshot — pre-fingerprint behaviour), or "auto" (embedding when
    # both languages have vectors, else prompt).
    mapping_candidate_source: str = "auto"
    # How many nearest candidates the embedding shortlist hands to the
    # attribution gate per source entry.
    mapping_candidate_top_k: int = 5
    # Settings for the "hoglah" adapter (model_adapter / embedding_adapter /
    # chat_adapter = "hoglah"): route calls through a Hoglah queue daemon
    # rather than calling Ollama directly. Unused by the other adapters.
    hoglah: HoglahRoutingConfig = Field(default_factory=lambda: HoglahRoutingConfig())


class HoglahRoutingConfig(BaseModel):
    """Settings for routing LLM calls through a Hoglah queue daemon instead of
    calling Ollama directly (model_adapter / embedding_adapter = "hoglah").

    Topology: a SEPARATE `hoglah run` worker daemon executes jobs off the
    shared SQLite queue (`db_path`) and writes each result to `output_dir`.
    Mahalath is a pure submitter — it enqueues a job, gets a job id, and either
    polls `output_dir/<job_id>.json` (delivery="poll") or receives an HTTP
    callback (delivery="callback", see M2). Both `db_path` and `output_dir`
    must match the daemon's own config.
    """

    db_path: str = "~/.hoglah/hoglah.db"
    output_dir: str = "~/.hoglah/outbox"
    # How a submitted job's result comes back: "poll" the output folder, or
    # "callback" (Mahalath runs a tiny receiver and Hoglah POSTs to it).
    delivery: str = "poll"
    # Poll cadence for the output folder (delivery="poll").
    poll_interval_seconds: float = 0.5
    # Callback receiver (delivery="callback"); 0 = pick an ephemeral port.
    callback_host: str = "127.0.0.1"
    callback_port: int = 0


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
