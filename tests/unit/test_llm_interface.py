from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bewerbungspilot.core.config import ModelProfile, load_models_config
from bewerbungspilot.core.errors import (
    GenerationTimeoutError,
    IncompleteGenerationError,
    ModelMismatchError,
    StructuredOutputError,
    UnsupportedCapabilityError,
)
from bewerbungspilot.llm.ollama import OllamaClient
from bewerbungspilot.llm.types import ImageRequest, TextRequest

APP_ROOT = Path(__file__).resolve().parents[2]


def response(text: str, *, model: str = "test:1b", reason: str = "stop") -> dict[str, Any]:
    return {
        "model": model,
        "response": text,
        "done": True,
        "done_reason": reason,
        "prompt_eval_count": 10,
        "eval_count": 5,
        "total_duration": 100,
    }


class FakeTransport:
    def __init__(self, responses: list[dict[str, Any] | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, Any] | None, int]] = []

    def request_json(
        self, method: str, path: str, payload: dict[str, Any] | None, timeout: int
    ) -> dict[str, Any]:
        self.calls.append((method, path, payload, timeout))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class MemoryFailureStore:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []

    def record(self, category: str, payload: dict[str, Any]) -> None:
        self.items.append((category, payload))
        return None


@pytest.fixture
def profile() -> ModelProfile:
    return ModelProfile(model="test:1b", timeout_seconds=7)


def test_example_config_is_valid_and_routes_known_profiles() -> None:
    config = load_models_config(APP_ROOT / "config/models.example.yaml")
    assert config.models["quick_vision"].supports_vision is True
    assert config.models["compact_critic"].think == "medium"
    assert config.runtime.silent_fallback is False


def test_health_reports_exact_model_availability(profile: ModelProfile) -> None:
    transport = FakeTransport([
        {"version": "1.2.3"},
        {"models": [{"name": "other:1b"}]},
    ])
    health = OllamaClient(profile, transport=transport).health()
    assert health.engine_available is True
    assert health.model_available is False
    assert health.configured_model == "test:1b"


def test_text_generation_returns_provider_neutral_metrics(profile: ModelProfile) -> None:
    transport = FakeTransport([response("Bonjour")])
    result = OllamaClient(profile, transport=transport).generate_text(TextRequest(prompt="Test"))
    assert result.text == "Bonjour"
    assert result.actual_model == "test:1b"
    assert result.usage.completion_tokens == 5
    assert transport.calls[0][3] == 7


def test_model_mismatch_never_falls_back_silently(profile: ModelProfile) -> None:
    transport = FakeTransport([response("x", model="other:1b")])
    with pytest.raises(ModelMismatchError):
        OllamaClient(profile, transport=transport).generate_text(TextRequest(prompt="Test"))
    assert len(transport.calls) == 1


def test_incomplete_generation_is_rejected_with_raw_response(profile: ModelProfile) -> None:
    raw = response("", reason="length")
    transport = FakeTransport([raw])
    with pytest.raises(IncompleteGenerationError) as caught:
        OllamaClient(profile, transport=transport).generate_text(TextRequest(prompt="Test"))
    assert caught.value.raw_response == raw


def test_structured_output_repairs_once_and_keeps_both_raws(profile: ModelProfile) -> None:
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    store = MemoryFailureStore()
    transport = FakeTransport([response("not-json"), response('{"ok": true}')])
    result = OllamaClient(profile, transport=transport, failure_store=store).generate_structured(
        TextRequest(prompt="Test"), schema
    )
    assert result.parsed == {"ok": True}
    assert result.repaired is True
    assert len(result.raw_responses) == 2
    assert len(transport.calls) == 2
    assert store.items[0][0] == "structured_initial"


def test_structured_output_never_repairs_twice(profile: ModelProfile) -> None:
    schema = {"type": "object", "required": ["ok"]}
    store = MemoryFailureStore()
    transport = FakeTransport([response("bad-1"), response("bad-2")])
    with pytest.raises(StructuredOutputError) as caught:
        OllamaClient(profile, transport=transport, failure_store=store).generate_structured(
            TextRequest(prompt="Test"), schema
        )
    assert len(transport.calls) == 2
    assert len(caught.value.raw_responses) == 2
    assert [item[0] for item in store.items] == ["structured_initial", "structured_repair"]


def test_timeout_is_typed_and_not_retried(profile: ModelProfile) -> None:
    transport = FakeTransport([GenerationTimeoutError("deadline")])
    with pytest.raises(GenerationTimeoutError):
        OllamaClient(profile, transport=transport).generate_text(TextRequest(prompt="Test"))
    assert len(transport.calls) == 1


def test_vision_requires_explicit_capability(profile: ModelProfile) -> None:
    client = OllamaClient(profile, transport=FakeTransport([]))
    with pytest.raises(UnsupportedCapabilityError):
        client.analyze_image(ImageRequest(prompt="Read", image_base64="YWJj"))


def test_vision_sends_one_image_when_enabled() -> None:
    profile = ModelProfile(model="test:1b", supports_vision=True)
    transport = FakeTransport([response("form")])
    result = OllamaClient(profile, transport=transport).analyze_image(
        ImageRequest(prompt="Read", image_base64="YWJj")
    )
    assert result.text == "form"
    assert transport.calls[0][2]["images"] == ["YWJj"]


def test_unload_targets_only_the_configured_model(profile: ModelProfile) -> None:
    transport = FakeTransport([{}])
    OllamaClient(profile, transport=transport).unload()
    payload = transport.calls[0][2]
    assert payload == {
        "model": "test:1b",
        "prompt": "",
        "stream": False,
        "keep_alive": 0,
    }
