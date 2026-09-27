"""Short local diagnostic: configuration, Ollama health and, with --live, one tiny call.

Usage (from ``app``):
    .\\.venv\\Scripts\\python.exe scripts\\diagnose.py          # offline + health
    .\\.venv\\Scripts\\python.exe scripts\\diagnose.py --live   # + one short JSON call per model

No personal data is sent. Every result is written to logs/technical with a run_id.
Exit code: 0 OK, 1 configuration error, 2 engine or model unavailable, 3 live call failed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bewerbungspilot.core.config import load_models_config  # noqa: E402
from bewerbungspilot.core.errors import BewerbungspilotError  # noqa: E402
from bewerbungspilot.core.journal import RunContext, TechnicalJournal  # noqa: E402
from bewerbungspilot.llm.ollama import OllamaClient, UrllibJsonTransport  # noqa: E402
from bewerbungspilot.llm.types import TextRequest  # noqa: E402

SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean", "const": True}},
    "required": ["ok"],
    "additionalProperties": False,
}


def config_path() -> Path:
    local = ROOT / "config/models.yaml"
    return local if local.exists() else ROOT / "config/models.example.yaml"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", action="store_true", help="run one short JSON call per model")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    args = parser.parse_args()

    run = RunContext(purpose="diagnostic" + (" live" if args.live else ""))
    report: dict[str, object] = {"run_id": str(run.run_id), "config": str(config_path().name)}
    with TechnicalJournal(ROOT / "logs") as journal:
        try:
            config = load_models_config(config_path())
        except Exception as exc:  # configuration must fail loudly
            journal.record(run, "diagnose", "config_invalid", error=str(exc))
            print(json.dumps({**report, "config_error": str(exc)}, ensure_ascii=False, indent=2))
            return 1
        journal.record(run, "diagnose", "config_ok", profiles=sorted(config.models))

        # One client per distinct model: profiles sharing a model are checked once.
        seen: dict[str, str] = {}
        for name, profile in config.models.items():
            seen.setdefault(profile.model, name)
        exit_code = 0
        checks = []
        for model, name in seen.items():
            client = OllamaClient(config.models[name], transport=UrllibJsonTransport(args.base_url))
            health = client.health()
            entry: dict[str, object] = {
                "profile": name,
                "model": model,
                "engine_available": health.engine_available,
                "model_available": health.model_available,
                "engine_version": health.engine_version,
                "error": health.error,
            }
            journal.record(run, "diagnose", "health", **entry)
            if not (health.engine_available and health.model_available):
                exit_code = max(exit_code, 2)
            elif args.live:
                started = time.perf_counter()
                try:
                    result = client.generate_structured(
                        TextRequest(
                            system="Diagnostic local sans donnée personnelle. Réponds uniquement en JSON.",
                            prompt='Retourne {"ok": true}.',
                            run_id=str(run.run_id),
                        ),
                        SCHEMA,
                    )
                    entry.update(
                        live_ok=result.parsed == {"ok": True},
                        repaired=result.repaired,
                        wall_seconds=round(result.wall_seconds, 3),
                        completion_tokens=result.usage.completion_tokens,
                    )
                    journal.record(run, "diagnose", "live_ok", duration_seconds=time.perf_counter() - started, **entry)
                except BewerbungspilotError as exc:
                    entry["live_error"] = f"{type(exc).__name__}: {exc}"
                    journal.record(run, "diagnose", "live_failed", error=str(exc), profile=name)
                    exit_code = max(exit_code, 3)
                finally:
                    try:
                        client.unload()
                    except BewerbungspilotError:
                        pass
            checks.append(entry)
        report["checks"] = checks
        report["status"] = {0: "OK", 2: "ENGINE_OR_MODEL_UNAVAILABLE", 3: "LIVE_CALL_FAILED"}[exit_code]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
