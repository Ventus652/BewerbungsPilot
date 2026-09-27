"""Inbox pipeline (phase 4.4): offers dropped in ``data/inbox`` become dossiers.

Between offers (never inside one) the pipeline honours Pause / Resume / Stop. Each offer is
processed once: a small ledger records file hashes, so nothing is deleted or re-processed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Callable

from bewerbungspilot.core.errors import BewerbungspilotError
from bewerbungspilot.core.journal import BusinessJournal, RunContext, TechnicalJournal
from bewerbungspilot.documents.dossier import create_dossier
from bewerbungspilot.documents.render import render_documents
from bewerbungspilot.jobs.evaluation import evaluate_offer
from bewerbungspilot.jobs.extraction import OfferExtraction, verify_extraction
from bewerbungspilot.memory.cv_library import CvDocument
from bewerbungspilot.memory.records import MemorySnapshot

from .control import RunControl, StopRequested

Extractor = Callable[[str, str], OfferExtraction]


def run_inbox(
    *,
    inbox: Path,
    dossiers: Path,
    logs: Path,
    memory: MemorySnapshot,
    library: list[CvDocument],
    extractor: Extractor,
    control: RunControl,
    past_applications: list[dict[str, Any]],
    as_of: date,
    poll_seconds: float = 1.0,
) -> list[dict[str, Any]]:
    ledger_path = inbox / ".processed.json"
    ledger: dict[str, Any] = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {}
    run = RunContext(purpose="inbox pipeline")
    results: list[dict[str, Any]] = []
    with TechnicalJournal(logs) as tech, BusinessJournal(logs) as business:
        for offer_file in sorted(inbox.glob("*.md")) + sorted(inbox.glob("*.txt")):
            digest = hashlib.sha256(offer_file.read_bytes()).hexdigest()
            if digest in ledger:
                continue
            try:
                control.checkpoint(poll_seconds=poll_seconds)
            except StopRequested:
                tech.record(run, "pipeline", "stopped_by_user", remaining=offer_file.name)
                results.append({"file": offer_file.name, "status": "STOPPED"})
                break
            text = offer_file.read_text(encoding="utf-8")
            outcome: dict[str, Any] = {"file": offer_file.name}
            try:
                verified = verify_extraction(extractor(text, offer_file.stem), text)
                evaluation = evaluate_offer(verified, text, memory, as_of=as_of)
                outcome.update(decision=evaluation.assessment.decision.value, score=evaluation.assessment.score)
                business.record_offer_decision(run, evaluation.assessment.job_offer_id, evaluation.assessment.decision.value,
                                               f"{offer_file.name}: " + "; ".join(i.statement for i in (evaluation.assessment.blockers or evaluation.assessment.unknowns or evaluation.assessment.matches)[:2]))
                dossier = create_dossier(offer_text=text, verified=verified, evaluation=evaluation, memory=memory, root=dossiers,
                                         as_of=as_of, past_applications=past_applications, run_id=run.run_id)
                for record in dossier.transitions:
                    business.record_transition(run, record)
                if dossier.application.state.value == "SELECTED" and library:
                    rendered = render_documents(dossier.folder, memory=memory, library=library, as_of=as_of,
                                                past_companies=[p.get("company") for p in past_applications], run_id=run.run_id)
                    if rendered.transition:
                        business.record_transition(run, rendered.transition)
                    outcome["state"] = rendered.application.state.value
                else:
                    outcome["state"] = dossier.application.state.value
                outcome.update(status="PREPARED", dossier=dossier.folder.name)
            except BewerbungspilotError as exc:
                outcome.update(status=type(exc).__name__, reason=str(exc))
                tech.record(run, "pipeline", "offer_not_prepared", error=str(exc), file=offer_file.name)
            ledger[digest] = {"file": offer_file.name, **{k: v for k, v in outcome.items() if k != "file"}}
            ledger_path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
            results.append(outcome)
    return results
