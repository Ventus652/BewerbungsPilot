"""Ollama adapter implementing the provider-neutral LLM contract."""

from __future__ import annotations

import json
import logging
import socket
import time
from typing import Any, Protocol
from uuid import UUID
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from jsonschema import Draft202012Validator

from bewerbungspilot.core.config import ModelProfile
from bewerbungspilot.core.errors import (
    EngineUnavailableError,
    GenerationTimeoutError,
    IncompleteGenerationError,
    ModelLoadError,
    ModelMismatchError,
    ModelNotAvailableError,
    StructuredOutputError,
    UnsupportedCapabilityError,
)

from .base import LLMClient
from .failure_store import FailureStore, FileFailureStore
from .types import GenerationResult, HealthResult, ImageRequest, TextRequest, UsageMetrics

LOGGER = logging.getLogger(__name__)
LOAD_CRASH_MARKERS = ("llama-server process has terminated", "CUDA error", "failed to load model")


class JsonTransport(Protocol):
    def request_json(
        self, method: str, path: str, payload: dict[str, Any] | None, timeout: int
    ) -> dict[str, Any]: ...


class UrllibJsonTransport:
    def __init__(self, base_url: str = "http://127.0.0.1:11434") -> None:
        self.base_url = base_url.rstrip("/")

    def request_json(
        self, method: str, path: str, payload: dict[str, Any] | None, timeout: int
    ) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", errors="replace")
            if exc.code == 404:
                raise ModelNotAvailableError(detail or "Configured model not available") from exc
            if any(marker in detail for marker in LOAD_CRASH_MARKERS):
                raise ModelLoadError(f"Ollama HTTP {exc.code} while loading model: {detail}") from exc
            raise EngineUnavailableError(f"Ollama HTTP {exc.code}: {detail}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise self._timeout_or_unreachable(timeout) from exc
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise self._timeout_or_unreachable(timeout) from exc
            raise EngineUnavailableError(f"Ollama unavailable: {exc.reason}") from exc
        except (OSError, ValueError) as exc:
            raise EngineUnavailableError(f"Invalid Ollama response: {exc}") from exc


    def _timeout_or_unreachable(self, timeout: int) -> Exception:
        """A timeout is a slow generation only if the engine still accepts connections.

        On Windows a closed local port can time out instead of refusing, so probe once.
        """

        parts = urlsplit(self.base_url)
        try:
            with socket.create_connection((parts.hostname or "127.0.0.1", parts.port or 80), timeout=1):
                pass
        except OSError:
            return EngineUnavailableError(f"Ollama unavailable: connection to {self.base_url} timed out")
        return GenerationTimeoutError(f"Ollama request exceeded {timeout}s")


class OllamaClient(LLMClient):
    def __init__(
        self,
        profile: ModelProfile,
        transport: JsonTransport | None = None,
        failure_store: FailureStore | None = None,
        load_retry_delay_seconds: float = 5.0,
    ) -> None:
        self.profile = profile
        self.transport = transport or UrllibJsonTransport()
        self.failure_store = failure_store or FileFailureStore()
        self.load_retry_delay_seconds = load_retry_delay_seconds

    def health(self) -> HealthResult:
        try:
            version = self.transport.request_json("GET", "/api/version", None, 15)
            tags = self.transport.request_json("GET", "/api/tags", None, 15)
        except (EngineUnavailableError, GenerationTimeoutError) as exc:
            return HealthResult(
                engine_available=False,
                model_available=False,
                configured_model=self.profile.model,
                error=str(exc),
            )
        installed = sorted(
            item.get("name", "") for item in tags.get("models", []) if item.get("name")
        )
        return HealthResult(
            engine_available=True,
            model_available=self.profile.model in installed,
            configured_model=self.profile.model,
            engine_version=version.get("version"),
            installed_models=installed,
            error=None if self.profile.model in installed else "Configured model is not installed",
        )

    def _payload(self, request: TextRequest) -> dict[str, Any]:
        return {
            "model": self.profile.model,
            "system": request.system,
            "prompt": request.prompt,
            "stream": False,
            "think": self.profile.think,
            "keep_alive": self.profile.keep_alive,
            "options": {
                "temperature": self.profile.temperature,
                "num_ctx": self.profile.num_ctx,
                "num_predict": self.profile.num_predict,
                "seed": self.profile.seed,
            },
        }

    def _generate(
        self, payload: dict[str, Any], run_id: str | None = None
    ) -> tuple[dict[str, Any], float]:
        started = time.perf_counter()
        try:
            raw = self.transport.request_json(
                "POST", "/api/generate", payload, self.profile.timeout_seconds
            )
        except ModelLoadError as exc:
            # The crash happened while loading, before any generation: one delayed retry.
            self._record_failure("model_load_crash", {"model": self.profile.model, "error": str(exc)}, run_id)
            LOGGER.warning("Model load crashed, retrying once in %ss", self.load_retry_delay_seconds)
            time.sleep(self.load_retry_delay_seconds)
            raw = self.transport.request_json(
                "POST", "/api/generate", payload, self.profile.timeout_seconds
            )
        wall = time.perf_counter() - started
        actual_model = raw.get("model")
        if actual_model and actual_model != self.profile.model:
            self._record_failure("model_mismatch", {"model": self.profile.model, "raw": raw}, run_id)
            raise ModelMismatchError(
                f"Configured {self.profile.model!r}, received {actual_model!r}; no fallback allowed"
            )
        if raw.get("done") is not True or raw.get("done_reason") not in (None, "stop"):
            self._record_failure(
                "incomplete_generation", {"model": self.profile.model, "raw": raw}, run_id
            )
            raise IncompleteGenerationError(
                f"Incomplete generation: done={raw.get('done')!r}, "
                f"reason={raw.get('done_reason')!r}",
                raw,
            )
        return raw, wall

    @staticmethod
    def _usage(raw: dict[str, Any]) -> UsageMetrics:
        return UsageMetrics(
            prompt_tokens=raw.get("prompt_eval_count"),
            completion_tokens=raw.get("eval_count"),
            total_duration_ns=raw.get("total_duration"),
            load_duration_ns=raw.get("load_duration"),
            prompt_duration_ns=raw.get("prompt_eval_duration"),
            completion_duration_ns=raw.get("eval_duration"),
        )

    def _result(
        self,
        raw: dict[str, Any],
        wall: float,
        *,
        parsed: Any = None,
        raw_responses: list[dict[str, Any]] | None = None,
        repaired: bool = False,
    ) -> GenerationResult:
        return GenerationResult(
            text=str(raw.get("response") or ""),
            parsed=parsed,
            actual_model=str(raw.get("model") or self.profile.model),
            wall_seconds=round(wall, 3),
            usage=self._usage(raw),
            raw_responses=raw_responses or [raw],
            repaired=repaired,
        )

    def generate_text(self, request: TextRequest) -> GenerationResult:
        raw, wall = self._generate(self._payload(request), request.run_id)
        return self._result(raw, wall)

    @staticmethod
    def _parse_and_validate(text: str, schema: dict[str, Any]) -> Any:
        parsed = json.loads(text)
        errors = sorted(Draft202012Validator(schema).iter_errors(parsed), key=str)
        if errors:
            summary = "; ".join(f"{error.json_path}: {error.message}" for error in errors[:10])
            raise ValueError(summary)
        return parsed

    def _record_failure(
        self, category: str, payload: dict[str, Any], run_id: str | None = None
    ) -> None:
        try:
            if run_id is None:
                path = self.failure_store.record(category, payload)
            else:
                path = self.failure_store.record(category, {**payload, "run_id": run_id}, UUID(run_id))
            LOGGER.warning("Stored raw LLM failure %s at %s", category, path)
        except Exception:
            LOGGER.exception("Could not persist raw LLM failure %s", category)

    def generate_structured(
        self, request: TextRequest, schema: dict[str, Any]
    ) -> GenerationResult:
        payload = self._payload(request)
        payload["format"] = schema
        raw, wall = self._generate(payload, request.run_id)
        raws = [raw]
        try:
            parsed = self._parse_and_validate(str(raw.get("response") or ""), schema)
            return self._result(raw, wall, parsed=parsed)
        except (ValueError, TypeError, json.JSONDecodeError) as first_error:
            self._record_failure(
                "structured_initial",
                {"error": str(first_error), "model": self.profile.model, "raw": raw},
                request.run_id,
            )
            if self.profile.max_structured_repairs == 0:
                raise StructuredOutputError(str(first_error), raws) from first_error

        LOGGER.warning("Running the single permitted structured-output repair")
        repair_request = TextRequest(
            system=(
                "Répare uniquement le JSON fourni. Retourne un seul objet JSON conforme au "
                "schéma imposé, sans commentaire et sans ajouter de faits."
            ),
            prompt="Sortie invalide à réparer:\n" + str(raw.get("response") or ""),
            request_id=request.request_id,
            run_id=request.run_id,
            metadata=request.metadata,
        )
        repair_payload = self._payload(repair_request)
        repair_payload["format"] = schema
        repaired_raw, repair_wall = self._generate(repair_payload, request.run_id)
        raws.append(repaired_raw)
        try:
            parsed = self._parse_and_validate(
                str(repaired_raw.get("response") or ""), schema
            )
            return self._result(
                repaired_raw,
                wall + repair_wall,
                parsed=parsed,
                raw_responses=raws,
                repaired=True,
            )
        except (ValueError, TypeError, json.JSONDecodeError) as second_error:
            self._record_failure(
                "structured_repair",
                {
                    "error": str(second_error),
                    "model": self.profile.model,
                    "raw_responses": raws,
                },
                request.run_id,
            )
            raise StructuredOutputError(str(second_error), raws) from second_error

    def analyze_image(self, request: ImageRequest) -> GenerationResult:
        if not self.profile.supports_vision:
            raise UnsupportedCapabilityError(
                f"Model profile {self.profile.model!r} does not enable vision"
            )
        payload = self._payload(request)
        payload["images"] = [request.image_base64]
        raw, wall = self._generate(payload, request.run_id)
        return self._result(raw, wall)

    def unload(self) -> None:
        """Release this exact model without changing to another configured model."""

        self.transport.request_json(
            "POST",
            "/api/generate",
            {"model": self.profile.model, "prompt": "", "stream": False, "keep_alive": 0},
            min(self.profile.timeout_seconds, 90),
        )
