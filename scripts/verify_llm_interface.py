"""Small live verification of the provider-neutral Ollama interface; no personal data."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bewerbungspilot.core.config import load_models_config
from bewerbungspilot.llm.ollama import OllamaClient
from bewerbungspilot.llm.types import TextRequest


SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean", "const": True},
        "role": {"type": "string"},
    },
    "required": ["ok", "role"],
    "additionalProperties": False,
}


def main() -> int:
    config = load_models_config(ROOT / "config/models.example.yaml")
    outcomes = []
    for name in ("quick_vision", "strict_text"):
        client = OllamaClient(config.models[name])
        health = client.health()
        if not health.engine_available or not health.model_available:
            print(json.dumps(health.model_dump(), ensure_ascii=False, indent=2))
            return 2
        try:
            result = client.generate_structured(
                TextRequest(
                    system="Test local sans donnée personnelle. Retourne uniquement le JSON demandé.",
                    prompt=f"Confirme le fonctionnement et mets exactement le rôle {name!r} dans role.",
                ),
                SCHEMA,
            )
            outcomes.append(
                {
                    "profile": name,
                    "configured_model": config.models[name].model,
                    "actual_model": result.actual_model,
                    "parsed": result.parsed,
                    "repaired": result.repaired,
                    "wall_seconds": result.wall_seconds,
                    "completion_tokens": result.usage.completion_tokens,
                }
            )
        finally:
            client.unload()
    print(json.dumps(outcomes, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
