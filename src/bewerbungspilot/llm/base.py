"""Abstract model interface used by the rest of BewerbungsPilot."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .types import GenerationResult, HealthResult, ImageRequest, TextRequest


class LLMClient(ABC):
    @abstractmethod
    def health(self) -> HealthResult:
        """Return engine and configured-model availability."""

    @abstractmethod
    def generate_text(self, request: TextRequest) -> GenerationResult:
        """Generate free text with the configured model only."""

    @abstractmethod
    def generate_structured(
        self, request: TextRequest, schema: dict[str, Any]
    ) -> GenerationResult:
        """Generate and validate JSON, with at most one explicit repair."""

    @abstractmethod
    def analyze_image(self, request: ImageRequest) -> GenerationResult:
        """Analyze an image when the configured profile declares vision support."""
