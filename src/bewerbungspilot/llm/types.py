"""Provider-neutral request and response contracts."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    system: str = ""
    request_id: str | None = None
    run_id: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class ImageRequest(TextRequest):
    image_base64: str = Field(min_length=1)
    media_type: str = "image/png"


class UsageMetrics(BaseModel):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_duration_ns: int | None = None
    load_duration_ns: int | None = None
    prompt_duration_ns: int | None = None
    completion_duration_ns: int | None = None


class GenerationResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    text: str
    parsed: Any | None = None
    actual_model: str
    wall_seconds: float
    usage: UsageMetrics
    raw_responses: list[dict[str, Any]]
    repaired: bool = False
    status: str = "SUCCESS"


class HealthResult(BaseModel):
    engine_available: bool
    model_available: bool
    configured_model: str
    engine_version: str | None = None
    installed_models: list[str] = Field(default_factory=list)
    error: str | None = None
