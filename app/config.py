"""Application settings, loaded from environment variables and the project `.env` file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

import os
import secrets
import socket

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Project root (the directory containing `app/`). Used to locate `.env` regardless of CWD.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _default_worker_id() -> str:
    """hostname-pid-random: unique per engine process (used as task lease owner)."""
    return f"{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(3)}"


class Settings(BaseSettings):
    """Typed configuration. Environment variables take precedence over `.env` values."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+asyncpg://agentflow:agentflow@localhost:5432/agentflow"
    test_database_url: str = (
        "postgresql+asyncpg://agentflow:agentflow@localhost:5432/agentflow_test"
    )

    # SecretStr keeps the key out of repr()/logs. Optional until Part 2.
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3.5-flash"
    gemini_timeout_seconds: float = 60.0
    gemini_temperature: float = 0.3
    # Multi-provider AI + cost control (Part 8). Keys are optional: a provider without one is skipped.
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    ollama_base_url: str = "http://localhost:11434/v1"
    llm_timeout_seconds: float = Field(default=120.0, gt=0)  # Anthropic / OpenAI / Ollama calls
    models_config_path: Path = Path("config/models.yaml")
    pricing_config_path: Path = Path("config/pricing.yaml")
    quota_timezone: str = "UTC"  # Google resets daily quotas at midnight America/Los_Angeles
    default_max_cost_usd: float | None = Field(default=None, ge=0)
    default_max_tokens: int | None = Field(default=None, ge=1)
    default_max_llm_calls: int | None = Field(default=50, ge=1)  # safety net against runaway loops
    default_max_duration_seconds: float | None = Field(default=None, gt=0)
    default_estimated_output_tokens: int = Field(default=1500, ge=1)  # pre-call cost estimate
    # Model for the worker agents (researcher, analyst, writer, reviewer, Quick Ask). Empty = GEMINI_MODEL.
    # A lite model is ~10x faster and has its own free quota; the planner keeps GEMINI_MODEL.
    gemini_worker_model: str | None = None
    # Gemini's built-in Google Search (needs billing). Off by default: this key has no grounding quota.
    enable_search_grounding: bool = False
    # Free live web research (app.search.web): auto = Tavily if TAVILY_API_KEY else ddgs (no key).
    web_search_provider: Literal["auto", "ddgs", "tavily", "none"] = "auto"
    web_search_backends: str = "yahoo, bing, yandex, google"  # ddgs engines, tried one by one
    tavily_api_key: SecretStr | None = None
    web_max_results: int = Field(default=6, ge=1, le=20)  # search results per query
    web_max_pages: int = Field(default=4, ge=1, le=10)  # pages read and given to the model
    web_page_chars: int = Field(default=3500, ge=500, le=20000)  # text kept per page
    web_fetch_timeout_seconds: float = Field(default=6.0, gt=0)
    # Budget for upstream task outputs pasted into an agent prompt.
    max_context_chars: int = 20000
    # Upper bound on tasks in a plan (Planner-generated or manually supplied).
    max_plan_tasks: int = 12
    # Extra planner calls allowed to fix an invalid plan (total calls = 1 + this).
    planner_max_repair_attempts: int = 2
    # Gemini 3+ thinking level per role; ignored (logged once) for models without support.
    planner_thinking_level: Literal["minimal", "low", "medium", "high"] = "high"
    worker_thinking_level: Literal["minimal", "low", "medium", "high"] = "low"

    # Execution engine (Part 4). Tests set ENGINE_ENABLED=false and drive the engine manually.
    engine_enabled: bool = True
    engine_max_concurrency: int = Field(default=4, ge=1)
    engine_poll_interval_seconds: float = Field(default=1.0, gt=0)
    task_timeout_seconds: float = Field(default=180.0, gt=0)
    engine_shutdown_grace_seconds: float = Field(default=10.0, ge=0)
    engine_worker_id: str = Field(default_factory=_default_worker_id)

    # Reliability (Part 5)
    llm_provider: Literal["gemini", "fake"] = "gemini"
    fake_llm_delay_seconds: float = Field(default=3.0, ge=0)
    # Comma-separated in .env, e.g. GEMINI_FALLBACK_MODELS=gemini-3.5-flash-lite,gemini-3.1-flash-lite
    gemini_fallback_models: Annotated[list[str], NoDecode] = Field(default_factory=list)
    model_circuit_failure_threshold: int = Field(default=3, ge=1)
    model_circuit_cooldown_seconds: float = Field(default=120.0, ge=0)
    retry_base_delay_seconds: float = Field(default=2.0, ge=0)
    retry_max_delay_seconds: float = Field(default=60.0, ge=0)
    retry_jitter: bool = True
    default_task_max_attempts: int = Field(default=3, ge=1, le=20)
    lease_duration_seconds: float = Field(default=30.0, gt=0)
    heartbeat_interval_seconds: float = Field(default=10.0, gt=0)
    engine_single_node: bool = True
    chaos_crash_delay_seconds: float = Field(default=2.0, ge=0)

    # Human controls (Part 6)
    # Required in production; in development an unset key means the control API is open.
    admin_api_key: SecretStr | None = None
    approval_timeout_hours: float = Field(default=24.0, ge=0)  # 0 = approvals never expire
    approval_webhook_url: str | None = None
    publish_dir: Path = PROJECT_ROOT / "outputs" / "published"

    # Tools + safe actions (Part 9). No tools.yaml = no agent has tools (Part 8 behaviour).
    tools_config_path: Path = PROJECT_ROOT / "config" / "tools.yaml"
    max_tool_steps: int = Field(default=8, ge=1, le=50)
    max_tool_output_chars: int = Field(default=4000, ge=200)
    tool_loop_timeout_seconds: float = Field(default=900.0, gt=0)  # replaces TASK_TIMEOUT for tool agents
    tool_result_reuse_seconds: int = Field(default=3600, ge=0)  # reads reused from the journal this long
    workspaces_dir: Path = PROJECT_ROOT / "outputs" / "workspaces"
    outbox_dir: Path = PROJECT_ROOT / "outputs" / "outbox"
    sample_data_url: str = "postgresql+asyncpg://agentflow_reader:reader@localhost:5432/agentflow_sample"
    sample_db_reader_password: SecretStr = SecretStr("reader")

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("gemini_fallback_models", mode="before")
    @classmethod
    def _split_models(cls, value: object) -> object:
        if isinstance(value, str):
            return [m.strip() for m in value.split(",") if m.strip()]
        return value
    app_env: Literal["development", "test", "production"] = "development"

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("admin_api_key", "approval_webhook_url", "gemini_worker_model", "tavily_api_key",
                     "anthropic_api_key", "openai_api_key", "default_max_cost_usd", "default_max_tokens",
                     "default_max_llm_calls", "default_max_duration_seconds", mode="before")
    @classmethod
    def _empty_is_none(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def _production_needs_admin_key(self) -> Settings:
        if self.app_env == "production" and self.admin_api_key is None:
            raise ValueError("ADMIN_API_KEY is required when APP_ENV=production")
        return self

    @field_validator("gemini_api_key", mode="before")
    @classmethod
    def _empty_key_is_none(cls, value: object) -> object:
        # Treat an empty string or the .env.example placeholder as "not configured".
        if isinstance(value, str) and value.strip() in ("", "your-gemini-api-key-here"):
            return None
        return value


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()
