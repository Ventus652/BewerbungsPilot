"""Validated model configuration loaded independently from the Ollama adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


ThinkValue = bool | Literal["low", "medium", "high"]


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["ollama"] = "ollama"
    model: str = Field(min_length=1)
    think: ThinkValue = False
    timeout_seconds: int = Field(default=180, ge=1, le=1200)
    temperature: float = Field(default=0.2, ge=0, le=2)
    num_ctx: int = Field(default=8192, ge=2048)
    num_predict: int = Field(default=4096, ge=1)
    seed: int = 42
    keep_alive: str = "15m"
    supports_vision: bool = False
    max_structured_repairs: Literal[0, 1] = 1
    max_calls_per_application: int | None = Field(default=None, ge=1)


class RuntimePolicy(BaseModel):
    model_config = ConfigDict(extra="allow")

    sequential_models: bool = True
    unload_after_task: bool = True
    silent_fallback: Literal[False] = False
    maximum_reasoning_enabled_by_default: Literal[False] = False
    application_target_minutes: int = Field(default=15, ge=1)
    application_hard_limit_minutes: int = Field(default=20, ge=1)


class ModelsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: dict[str, ModelProfile]
    routing: dict[str, str]
    runtime: RuntimePolicy

    @model_validator(mode="after")
    def routes_reference_known_profiles(self) -> "ModelsConfig":
        unknown = sorted(set(self.routing.values()) - set(self.models))
        if unknown:
            raise ValueError(f"Routing references unknown model profiles: {unknown}")
        return self


def load_models_config(path: str | Path) -> ModelsConfig:
    """Load and validate a YAML model configuration without accepting secret fields."""

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return ModelsConfig.model_validate(raw)
