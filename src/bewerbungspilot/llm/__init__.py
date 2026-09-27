"""Model-independent LLM contracts and local adapters."""

from .base import LLMClient
from .ollama import OllamaClient
from .types import GenerationResult, HealthResult, ImageRequest, TextRequest

__all__ = [
    "GenerationResult",
    "HealthResult",
    "ImageRequest",
    "LLMClient",
    "OllamaClient",
    "TextRequest",
]
