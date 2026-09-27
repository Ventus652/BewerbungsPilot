"""Phase 3 gate: a local model analyses an offer using only the minimal context.

Usage (from ``app``, Ollama running):
    .\\.venv\\Scripts\\python.exe scripts\\analyze_offer_with_memory.py
    .\\.venv\\Scripts\\python.exe scripts\\analyze_offer_with_memory.py --context-only
    .\\.venv\\Scripts\\python.exe scripts\\analyze_offer_with_memory.py --case B02

Uses a fictional benchmark offer (default B01) and the private memory in data/private.
Exit code: 0 OK, 1 no memory, 2 engine/model unavailable, 3 model answer failed checks.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bewerbungspilot.core.config import load_models_config  # noqa: E402
from bewerbungspilot.core.errors import BewerbungspilotError  # noqa: E402
from bewerbungspilot.core.journal import RunContext, TechnicalJournal  # noqa: E402
from bewerbungspilot.llm.ollama import OllamaClient  # noqa: E402
from bewerbungspilot.memory.analysis import build_offer_request, review_model_analysis  # noqa: E402
from bewerbungspilot.memory.context import TaskType, build_context  # noqa: E402
from bewerbungspilot.memory.rules import ProfileRules  # noqa: E402
from bewerbungspilot.memory.store import MemoryStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyse a fictional offer with the minimal context")
    parser.add_argument("--case", default="B01")
    parser.add_argument("--profile", default="strict_text")
    parser.add_argument("--context-only", action="store_true")
    args = parser.parse_args()

    case = json.loads((ROOT / "benchmarks" / "cases" / f"{args.case}.json").read_text(encoding="utf-8"))
    memory = MemoryStore(ROOT / "data" / "private").load()
    if not memory.facts:
        print("Mémoire privée vide : lancer scripts/import_memory.py")
        return 1
    today = date.today()
    packet = build_context(memory, TaskType.OFFER_ANALYSIS, as_of=today, query=case["input"])
    run = RunContext(purpose=f"offer analysis {args.case}")
    request = build_offer_request(packet, case["input"], case_id=args.case, run_id=str(run.run_id))
    report: dict[str, object] = {
        "run_id": str(run.run_id),
        "case": args.case,
        "context_facts": len(packet.facts),
        "context_sources": len(packet.sources),
        "prompt_characters": len(request.prompt),
        "projects_offered": packet.ranked_projects,
    }
    if args.context_only:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    config = load_models_config(ROOT / "config" / "models.example.yaml")
    schema = json.loads((ROOT / "benchmarks" / "schemas" / "match.schema.json").read_text(encoding="utf-8"))
    client = OllamaClient(config.models[args.profile])
    with TechnicalJournal(ROOT / "logs") as journal:
        health = client.health()
        if not (health.engine_available and health.model_available):
            journal.record(run, "analyze_offer", "unavailable", error=health.error)
            print(json.dumps({**report, "error": health.error}, ensure_ascii=False, indent=2))
            return 2
        try:
            result = client.generate_structured(request, schema)
        except BewerbungspilotError as exc:
            journal.record(run, "analyze_offer", "model_failed", error=str(exc))
            print(json.dumps({**report, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False, indent=2))
            return 3
        finally:
            client.unload()
        issues = review_model_analysis(result.parsed, packet, ProfileRules(memory, as_of=today))
        report.update(
            model=result.actual_model,
            wall_seconds=round(result.wall_seconds, 3),
            repaired=result.repaired,
            analysis=result.parsed,
            deterministic_issues=issues,
        )
        journal.record(run, "analyze_offer", "done", duration_seconds=result.wall_seconds,
                       issues=len(issues), decision=result.parsed.get("decision"))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not issues else 3


if __name__ == "__main__":
    raise SystemExit(main())
