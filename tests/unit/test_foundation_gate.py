"""Phase 2.7 — foundation checks not covered elsewhere."""

import json
import socket
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from bewerbungspilot.core.config import ModelProfile, load_models_config
from bewerbungspilot.core.errors import (
    EngineUnavailableError,
    IncompleteGenerationError,
    StructuredOutputError,
)
from bewerbungspilot.domain import JobOffer, PublicationDateReliability
from bewerbungspilot.jobs.fingerprint import canonicalize_url, offer_fingerprint
from bewerbungspilot.llm.failure_store import FileFailureStore
from bewerbungspilot.llm.ollama import OllamaClient, UrllibJsonTransport
from bewerbungspilot.llm.types import TextRequest

APP_ROOT = Path(__file__).resolve().parents[2]
GOLDEN = "82cbe94b85f859c8d2636cbabdd93b5ac4ca17a5ff685da0695cfdbe9bb18990"


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ScriptedTransport:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)

    def request_json(self, method: str, path: str, payload: Any, timeout: int) -> dict[str, Any]:
        return self.responses.pop(0)


def raw(text: str, reason: str = "stop") -> dict[str, Any]:
    return {"model": "test:1b", "response": text, "done": True, "done_reason": reason}


# ---------------------------------------------------------------- configuration
def test_both_example_configs_load() -> None:
    import yaml

    models = load_models_config(APP_ROOT / "config/models.example.yaml")
    assert {"quick_vision", "strict_text"} <= set(models.models)
    settings = yaml.safe_load((APP_ROOT / "config/settings.example.yaml").read_text(encoding="utf-8"))
    safety = settings["safety"]
    assert safety["allow_real_browser_actions"] is False
    assert safety["allow_external_connectors"] is False
    assert safety["require_confirmation_before_submission"] is True


# ---------------------------------------------------------------- Ollama unavailable
def test_unreachable_engine_raises_a_clear_typed_error() -> None:
    transport = UrllibJsonTransport(f"http://127.0.0.1:{closed_port()}")
    client = OllamaClient(ModelProfile(model="test:1b", timeout_seconds=2), transport=transport)
    with pytest.raises(EngineUnavailableError) as caught:
        client.generate_text(TextRequest(prompt="ping"))
    assert "unavailable" in str(caught.value).lower()


def test_unreachable_engine_health_is_explicit_not_empty() -> None:
    transport = UrllibJsonTransport(f"http://127.0.0.1:{closed_port()}")
    health = OllamaClient(ModelProfile(model="test:1b", timeout_seconds=2), transport=transport).health()
    assert health.engine_available is False and health.model_available is False
    assert health.error


# ---------------------------------------------------------------- deduplication fingerprint
def test_fingerprint_is_stable_across_runs() -> None:
    assert offer_fingerprint(
        "ACME Software GmbH", "Werkstudent Softwareentwicklung (m/w/d)", "60311 Frankfurt am Main"
    ) == GOLDEN


@pytest.mark.parametrize(
    ("employer", "title", "location"),
    [
        ("acme software", "Werkstudent Softwareentwicklung", "Frankfurt am Main"),
        ("ACME Software GmbH & Co. KG", "Werkstudent  Softwareentwicklung (w/m/d)", "Frankfurt am Main, Deutschland"),
        ("Acme Software AG", "WERKSTUDENT SOFTWAREENTWICKLUNG (all genders)", "60311  Frankfurt am Main"),
    ],
)
def test_same_offer_on_other_platforms_has_same_fingerprint(employer: str, title: str, location: str) -> None:
    assert offer_fingerprint(employer, title, location) == GOLDEN


@pytest.mark.parametrize(
    ("employer", "title", "location"),
    [
        ("ACME Software", "Werkstudent Data Engineering", "Frankfurt am Main"),
        ("ACME Software", "Werkstudent Softwareentwicklung", "Berlin"),
        ("Other Company", "Werkstudent Softwareentwicklung", "Frankfurt am Main"),
    ],
)
def test_different_offers_have_different_fingerprints(employer: str, title: str, location: str) -> None:
    assert offer_fingerprint(employer, title, location) != GOLDEN


def test_fingerprint_is_accepted_by_job_offer() -> None:
    offer = JobOffer(
        canonical_url=canonicalize_url("https://jobs.example.com/123/?utm_source=x"),
        platform="example",
        employer="ACME Software GmbH",
        title="Werkstudent Softwareentwicklung (m/w/d)",
        location="Frankfurt am Main",
        employment_type="Werkstudent",
        publication_date_reliability=PublicationDateReliability.UNKNOWN,
        raw_description="Fictional offer",
        deduplication_fingerprint=offer_fingerprint("ACME Software GmbH", "Werkstudent Softwareentwicklung (m/w/d)", "Frankfurt am Main"),
    )
    assert str(offer.canonical_url) == "https://jobs.example.com/123"


def test_url_canonicalisation_drops_tracking_only() -> None:
    assert canonicalize_url("HTTPS://Jobs.Example.com/job/1/?utm_source=a&b=2&a=1#x") == "https://jobs.example.com/job/1?a=1&b=2"


# ---------------------------------------------------------------- raw responses kept on failure
def test_raw_responses_are_written_to_disk_with_run_id(tmp_path: Path) -> None:
    run_id = str(uuid4())
    client = OllamaClient(
        ModelProfile(model="test:1b"),
        transport=ScriptedTransport([raw("not json"), raw("still not json")]),
        failure_store=FileFailureStore(tmp_path),
    )
    with pytest.raises(StructuredOutputError):
        client.generate_structured(TextRequest(prompt="x", run_id=run_id), {"type": "object"})
    stored = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(tmp_path.iterdir())]
    assert len(stored) == 2 and {item["run_id"] for item in stored} == {run_id}
    texts = json.dumps(stored)
    assert "not json" in texts and "still not json" in texts


def test_truncated_generation_is_also_kept(tmp_path: Path) -> None:
    client = OllamaClient(
        ModelProfile(model="test:1b"),
        transport=ScriptedTransport([raw("partial", reason="length")]),
        failure_store=FileFailureStore(tmp_path),
    )
    with pytest.raises(IncompleteGenerationError):
        client.generate_text(TextRequest(prompt="x"))
    (stored,) = [json.loads(p.read_text(encoding="utf-8")) for p in tmp_path.iterdir()]
    assert stored["category"] == "incomplete_generation"
    assert stored["payload"]["raw"]["response"] == "partial"


# ---------------------------------------------------------------- Windows findings (26/09/2026)
from bewerbungspilot.core.errors import GenerationTimeoutError, ModelLoadError  # noqa: E402


class CrashThenOk:
    def __init__(self, crashes: int) -> None:
        self.crashes = crashes
        self.calls = 0

    def request_json(self, method: str, path: str, payload: Any, timeout: int) -> dict[str, Any]:
        self.calls += 1
        if self.calls <= self.crashes:
            raise ModelLoadError("llama-server process has terminated: CUDA error")
        return raw("ok")


def test_transient_model_load_crash_is_retried_once(tmp_path: Path) -> None:
    transport = CrashThenOk(crashes=1)
    client = OllamaClient(ModelProfile(model="test:1b"), transport=transport,
                          failure_store=FileFailureStore(tmp_path), load_retry_delay_seconds=0)
    assert client.generate_text(TextRequest(prompt="x")).text == "ok"
    assert transport.calls == 2
    (stored,) = [json.loads(p.read_text(encoding="utf-8")) for p in tmp_path.iterdir()]
    assert stored["category"] == "model_load_crash"


def test_repeated_model_load_crash_is_not_retried_forever(tmp_path: Path) -> None:
    transport = CrashThenOk(crashes=5)
    client = OllamaClient(ModelProfile(model="test:1b"), transport=transport,
                          failure_store=FileFailureStore(tmp_path), load_retry_delay_seconds=0)
    with pytest.raises(ModelLoadError):
        client.generate_text(TextRequest(prompt="x"))
    assert transport.calls == 2


def test_slow_but_alive_engine_is_a_timeout_not_unavailable() -> None:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(16)  # accepts connections, never answers (room for the probe on Windows)
        transport = UrllibJsonTransport(f"http://127.0.0.1:{server.getsockname()[1]}")
        client = OllamaClient(ModelProfile(model="test:1b", timeout_seconds=1), transport=transport)
        with pytest.raises(GenerationTimeoutError):
            client.generate_text(TextRequest(prompt="x"))
