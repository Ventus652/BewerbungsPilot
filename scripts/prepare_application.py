"""Prepare one isolated application dossier (phase 4.2). Nothing is sent.

Usage (from ``app``):
    .\\.venv\\Scripts\\python.exe scripts\\prepare_application.py --case A01
    .\\.venv\\Scripts\\python.exe scripts\\prepare_application.py --text annonce.md --live
Options: --salary (include the validated salary if the offer asks for it),
         --root (default data/generated/applications).
Exit code: 0 prepared, 1 input problem, 2 rejected or duplicate, 3 extraction failed.
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
from bewerbungspilot.core.journal import BusinessJournal, RunContext  # noqa: E402
from bewerbungspilot.documents.dossier import DuplicateOfferError, RejectedOfferError, create_dossier  # noqa: E402
from bewerbungspilot.jobs.evaluation import evaluate_offer  # noqa: E402
from bewerbungspilot.jobs.extraction import OfferExtraction, extract_with_model, verify_extraction  # noqa: E402
from bewerbungspilot.llm.ollama import OllamaClient  # noqa: E402
from bewerbungspilot.memory.store import MemoryStore  # noqa: E402

BENCH = ROOT / "benchmarks"


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare an application dossier")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--case")
    source.add_argument("--text", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--salary", action="store_true")
    parser.add_argument("--no-pdf", action="store_true", help="skip PDF rendering")
    parser.add_argument("--root", type=Path, default=ROOT / "data" / "generated" / "applications")
    args = parser.parse_args()

    offer = (json.loads((BENCH / "cases" / f"{args.case}.json").read_text(encoding="utf-8"))["input"]
             if args.case else args.text.read_text(encoding="utf-8"))
    store = MemoryStore(ROOT / "data" / "private")
    memory = store.load()
    if not memory.facts:
        print("Mémoire vide : lancer scripts/import_memory.py")
        return 1
    index_path = store.root / "applications_index.json"
    past = json.loads(index_path.read_text(encoding="utf-8"))["applications"] if index_path.exists() else []
    run = RunContext(purpose=f"prepare application {args.case or args.text.name}")

    if args.live:
        client = OllamaClient(load_models_config(ROOT / "config" / "models.example.yaml").models["strict_text"])
        try:
            extraction = extract_with_model(client, offer, case_id=args.case or "X00", run_id=str(run.run_id))
        except BewerbungspilotError as exc:
            print(json.dumps({"status": "EXTRACTION_FAILED", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False, indent=2))
            return 3
        finally:
            try:
                client.unload()
            except BewerbungspilotError:
                pass
    else:
        reference = BENCH / "expected_reviewed" / f"{args.case}.json"
        data = json.loads(reference.read_text(encoding="utf-8")) if args.case and reference.exists() else {}
        if "company" not in data:
            print("Pas d'extraction de référence : utiliser --live")
            return 1
        extraction = OfferExtraction.model_validate(data)

    verified = verify_extraction(extraction, offer)
    evaluation = evaluate_offer(verified, offer, memory, as_of=date.today())
    try:
        result = create_dossier(offer_text=offer, verified=verified, evaluation=evaluation, memory=memory, root=args.root,
                                as_of=date.today(), past_applications=past, include_salary=args.salary, run_id=run.run_id)
    except (RejectedOfferError, DuplicateOfferError) as exc:
        with BusinessJournal(ROOT / "logs") as journal:
            journal.record_offer_decision(run, evaluation.assessment.job_offer_id, evaluation.assessment.decision.value, str(exc))
        print(json.dumps({"status": type(exc).__name__, "reason": str(exc), "decision": evaluation.assessment.decision.value,
                          "score": evaluation.assessment.score}, ensure_ascii=False, indent=2))
        return 2
    except BewerbungspilotError as exc:  # e.g. company or title missing from the offer
        print(json.dumps({"status": "NOT_PREPARED", "reason": str(exc), "decision": evaluation.assessment.decision.value,
                          "score": evaluation.assessment.score}, ensure_ascii=False, indent=2))
        return 2
    with BusinessJournal(ROOT / "logs") as journal:
        for record in result.transitions:
            journal.record_transition(run, record)
    if not args.no_pdf and result.application.state.value == "SELECTED":
        import render_documents  # same folder; renders, verifies and records DOCUMENTS_PREPARED

        print(json.dumps({"status": "PREPARED", "folder": str(result.folder.name), "letter_check": "OK" if result.letter_check.ok else "REFUSED"},
                         ensure_ascii=False))
        return render_documents.run(result.folder)
    print(json.dumps({
        "status": "PREPARED",
        "folder": str(result.folder.relative_to(ROOT)) if result.folder.is_relative_to(ROOT) else str(result.folder),
        "state": result.application.state.value,
        "decision": evaluation.assessment.decision.value,
        "score": evaluation.assessment.score,
        "letter_check": "OK" if result.letter_check.ok else [f"{i.rule}: {i.detail}" for i in result.letter_check.issues],
        "removed_from_extraction": verified.removed,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
