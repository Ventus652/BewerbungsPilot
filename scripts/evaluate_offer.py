"""Evaluate an offer: the model extracts, the code decides (phase 4.1).

Usage (from ``app``):
    .\\.venv\\Scripts\\python.exe scripts\\evaluate_offer.py --case B01            # reference extraction, offline
    .\\.venv\\Scripts\\python.exe scripts\\evaluate_offer.py --case B01 --live     # GPT-OSS extraction
    .\\.venv\\Scripts\\python.exe scripts\\evaluate_offer.py --text annonce.md --live
Options: --memory private|benchmark (default private).
Exit code: 0 evaluated, 1 input/memory problem, 2 engine unavailable, 3 extraction failed.
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
from bewerbungspilot.core.journal import BusinessJournal, RunContext, TechnicalJournal  # noqa: E402
from bewerbungspilot.jobs.evaluation import evaluate_offer  # noqa: E402
from bewerbungspilot.jobs.extraction import OfferExtraction, extract_with_model, verify_extraction  # noqa: E402
from bewerbungspilot.llm.ollama import OllamaClient  # noqa: E402
from bewerbungspilot.memory.benchmark import memory_from_benchmark_profile  # noqa: E402
from bewerbungspilot.memory.store import MemoryStore  # noqa: E402

BENCH = ROOT / "benchmarks"


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic offer evaluation")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--case")
    source.add_argument("--text", type=Path)
    parser.add_argument("--live", action="store_true", help="extract with the local model")
    parser.add_argument("--memory", choices=("private", "benchmark"), default="private")
    parser.add_argument("--profile", default="strict_text")
    args = parser.parse_args()

    case_id = args.case or "X00"
    offer = (json.loads((BENCH / "cases" / f"{args.case}.json").read_text(encoding="utf-8"))["input"]
             if args.case else args.text.read_text(encoding="utf-8"))
    memory = memory_from_benchmark_profile() if args.memory == "benchmark" else MemoryStore(ROOT / "data" / "private").load()
    if not memory.facts:
        print("Mémoire vide : lancer scripts/import_memory.py")
        return 1
    run = RunContext(purpose=f"offer evaluation {case_id}")
    report: dict[str, object] = {"run_id": str(run.run_id), "case": case_id, "memory": args.memory}

    with TechnicalJournal(ROOT / "logs") as tech, BusinessJournal(ROOT / "logs") as business:
        if args.live:
            client = OllamaClient(load_models_config(ROOT / "config" / "models.example.yaml").models[args.profile])
            try:
                extraction = extract_with_model(client, offer, case_id=case_id, run_id=str(run.run_id))
                report["extraction_source"] = client.profile.model
            except BewerbungspilotError as exc:
                tech.record(run, "evaluate_offer", "extraction_failed", error=str(exc))
                print(json.dumps({**report, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False, indent=2))
                return 2 if "unavailable" in str(exc).lower() else 3
            finally:
                try:
                    client.unload()
                except BewerbungspilotError:
                    pass
        else:
            reference = BENCH / "expected_reviewed" / f"{case_id}.json"
            if not args.case or not reference.exists() or json.loads(reference.read_text(encoding="utf-8")).get("company", "∅") == "∅":
                print("Pas d'extraction de référence pour ce cas : utiliser --live")
                return 1
            extraction = OfferExtraction.model_validate(json.loads(reference.read_text(encoding="utf-8")))
            report["extraction_source"] = "reference"
        verified = verify_extraction(extraction, offer)
        evaluation = evaluate_offer(verified, offer, memory, as_of=date.today())
        a = evaluation.assessment
        business.record_offer_decision(run, a.job_offer_id, a.decision.value,
                                       "; ".join(i.statement for i in (a.blockers or a.gaps or a.unknowns or a.matches)[:3]))
        tech.record(run, "evaluate_offer", "done", decision=a.decision.value, score=a.score, removed=len(verified.removed))
        report.update(
            decision=a.decision.value,
            score=a.score,
            next_state=evaluation.next_state.value,
            criteria=[f"{c.criterion}: {c.points}/{c.max_points} {c.verdict.value} — {c.explanation}" for c in evaluation.criteria],
            matches=[i.statement for i in a.matches],
            gaps=[i.statement for i in a.gaps],
            blockers=[i.statement for i in a.blockers],
            unknowns=[i.statement for i in a.unknowns],
            recommended_projects=a.recommended_projects,
            removed_from_extraction=verified.removed,
            notes=evaluation.notes,
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
