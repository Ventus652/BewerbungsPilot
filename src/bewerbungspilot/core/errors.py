"""Typed application errors; callers never need to parse exception strings."""

from __future__ import annotations

from typing import Any


class BewerbungspilotError(Exception):
    """Base class for expected application failures."""


class LLMError(BewerbungspilotError):
    """Base class for local model failures."""


class EngineUnavailableError(LLMError):
    """The local inference engine cannot be reached."""


class ModelLoadError(EngineUnavailableError):
    """The engine is up but crashed while loading the model (e.g. transient CUDA init failure).

    Loading is idempotent — no text was generated — so one delayed retry is safe.
    """


class ModelNotAvailableError(LLMError):
    """The configured model is not installed or cannot be loaded."""


class ModelMismatchError(LLMError):
    """The engine answered with a model other than the configured one."""


class GenerationTimeoutError(LLMError):
    """The configured request deadline was reached."""


class IncompleteGenerationError(LLMError):
    """The model stopped through truncation or without a completion signal."""

    def __init__(self, message: str, raw_response: dict[str, Any]):
        super().__init__(message)
        self.raw_response = raw_response


class StructuredOutputError(LLMError):
    """Structured output failed after the single permitted repair attempt."""

    def __init__(self, message: str, raw_responses: list[dict[str, Any]]):
        super().__init__(message)
        self.raw_responses = raw_responses


class UnsupportedCapabilityError(LLMError):
    """The configured model profile does not support the requested capability."""


class StateTransitionError(BewerbungspilotError):
    """Base class for rejected application state transitions.

    The application object is never modified when one of these errors is raised.
    """

    def __init__(self, message: str, *, from_state: str | None = None, to_state: str | None = None):
        super().__init__(message)
        self.from_state = from_state
        self.to_state = to_state


class ForbiddenTransitionError(StateTransitionError):
    """The requested arc is absent from the transition table or the source is terminal."""


class StaleStateError(StateTransitionError):
    """The caller's expected state or version no longer matches the application."""


class DuplicateTransitionError(StateTransitionError):
    """The same transition execution was already applied and must not be repeated."""


class PreconditionFailedError(StateTransitionError):
    """The target state's documents, evidence, authorization or checks are missing."""
