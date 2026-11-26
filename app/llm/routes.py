"""Model routes: which "provider:model" entries each agent role tries, in order (config/models.yaml).

routes:
  default: ["gemini:gemini-3.5-flash", "ollama:qwen3:4b"]   # required; used by roles without their own
  planner: [...]                                            # optional per role (planner, researcher,
quotas:                                                     # analyst, writer, reviewer, ask)
  "gemini:gemini-3.5-flash": {requests_per_day: 20, timezone: America/Los_Angeles}

No file -> the legacy Gemini route (GEMINI_MODEL + GEMINI_FALLBACK_MODELS, worker roles starting
with GEMINI_WORKER_MODEL), so a Gemini-only setup behaves exactly as before. LLM_PROVIDER=fake
routes every role to fake:fake-model. Validation happens once at startup with clear errors.
"""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from app.config import Settings

logger = logging.getLogger(__name__)

KNOWN_PROVIDERS = frozenset({"gemini", "anthropic", "openai", "ollama", "fake"})
WORKER_ROLES = ("researcher", "analyst", "writer", "reviewer", "ask")
FAKE_ENTRY = "fake:fake-model"


class RouteConfigError(ValueError):
    """Invalid models.yaml or a role with no usable model: the app must not start."""


class QuotaSpec(BaseModel):
    requests_per_day: int | None = Field(default=None, ge=1)
    requests_per_minute: int | None = Field(default=None, ge=1)
    timezone: str | None = None  # daily window timezone; default QUOTA_TIMEZONE

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError(f"unknown timezone {value!r}") from exc
        return value


def split_entry(entry: str) -> tuple[str, str]:
    """'ollama:qwen3:4b' -> ('ollama', 'qwen3:4b'): only the first ':' separates the provider."""
    provider, _, model = entry.partition(":")
    return provider, model


class RouteConfig(BaseModel):
    routes: dict[str, list[str]]
    quotas: dict[str, QuotaSpec] = Field(default_factory=dict)
    legacy: bool = False  # built from GEMINI_* settings (no models.yaml): v1 fallback rules apply

    @model_validator(mode="after")
    def _check(self) -> RouteConfig:
        if "default" not in self.routes:
            raise ValueError("routes.default is required (used by every role without its own route)")
        for role, entries in self.routes.items():
            if not entries:
                raise ValueError(f"route '{role}' is empty")
            for entry in entries:
                provider, model = split_entry(entry)
                if not provider or not model:
                    raise ValueError(f"route '{role}': entry {entry!r} must be written as 'provider:model'")
                if provider not in KNOWN_PROVIDERS:
                    raise ValueError(f"route '{role}': unknown provider '{provider}' in {entry!r} "
                                     f"(known: {', '.join(sorted(KNOWN_PROVIDERS))})")
        return self

    def route_for(self, role: str | None) -> list[str]:
        return self.routes.get(role or "default") or self.routes["default"]


def legacy_routes(settings: Settings) -> RouteConfig:
    chain = list(dict.fromkeys([settings.gemini_model, *settings.gemini_fallback_models]))
    routes = {"default": [f"gemini:{m}" for m in chain]}
    if settings.gemini_worker_model:
        # Same order the Part 7 worker chain used: worker model, other fallbacks, primary last.
        worker = list(dict.fromkeys([settings.gemini_worker_model, *chain[1:], chain[0]]))
        routes.update({role: [f"gemini:{m}" for m in worker] for role in WORKER_ROLES})
    # Google resets daily quotas at midnight Pacific: an exhausted window must reopen then, not at UTC midnight.
    entries = {e for route in routes.values() for e in route}
    quotas = {e: QuotaSpec(timezone="America/Los_Angeles") for e in entries}
    return RouteConfig(routes=routes, quotas=quotas, legacy=True)


def load_routes(settings: Settings, available: set[str]) -> RouteConfig:
    if settings.llm_provider == "fake":
        return RouteConfig(routes={"default": [FAKE_ENTRY]})
    path = settings.models_config_path
    if not path.is_file():
        logger.info("routes.legacy path=%s missing: Gemini-only routes from GEMINI_MODEL settings", path)
        return legacy_routes(settings)  # unfiltered: behaves exactly as before (a missing key fails the call)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RouteConfigError(f"Invalid YAML in {path} (models.yaml): {exc}") from exc
    try:
        cfg = RouteConfig.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors())
        raise RouteConfigError(f"Invalid model routes in {path} (models.yaml): {problems}") from exc

    usable: dict[str, list[str]] = {}
    for role, entries in cfg.routes.items():
        kept = []
        for entry in entries:
            if split_entry(entry)[0] in available:
                kept.append(entry)
            else:
                logger.warning("routes.entry_skipped role=%s entry=%s reason=provider not configured "
                               "(no key, or Ollama not running)", role, entry)
        if not kept:
            raise RouteConfigError(f"No usable model for role '{role}' in {path} (models.yaml): every entry's "
                                   f"provider is unconfigured ({', '.join(entries)}). Add a key or another entry.")
        usable[role] = kept
    return RouteConfig(routes=usable, quotas=cfg.quotas)
