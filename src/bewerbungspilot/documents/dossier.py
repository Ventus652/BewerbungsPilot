"""One isolated application folder per offer (phase 4.2).

Created under ``data/generated/applications`` (ignored by Git). Nothing is written into the
historical profile-source folders and nothing is sent. The folder keeps the
offer, the verified extraction, the code's evaluation, the fact pack, the checked letter,
the CV plan, the application state and a README following ``MODE_EMPLOI_CANDIDATURES.md``.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel

from bewerbungspilot.core.errors import BewerbungspilotError
from bewerbungspilot.domain.enums import ApplicationState as S, MatchDecision, TransitionActor
from bewerbungspilot.domain.models import Application
from bewerbungspilot.domain.state_machine import (
    ApplicationStateMachine,
    TransitionCommand,
    TransitionContext,
    TransitionRecord,
)
from bewerbungspilot.jobs.evaluation import OfferEvaluation
from bewerbungspilot.jobs.extraction import VerifiedExtraction
from bewerbungspilot.jobs.fingerprint import offer_fingerprint
from bewerbungspilot.memory.context import TaskType, build_context
from bewerbungspilot.memory.parsing import slug
from bewerbungspilot.memory.records import MemorySnapshot
from bewerbungspilot.memory.rules import ProfileRules

from .checks import LetterCheck, check_letter
from .cv_plan import build_cv_plan
from .dossier_readme import refresh_readme
from .letters import build_letter


class DuplicateOfferError(BewerbungspilotError):
    """The offer already has a dossier or a past application."""


class RejectedOfferError(BewerbungspilotError):
    """The evaluation rejected the offer: no dossier is prepared."""


class DossierResult(BaseModel):
    folder: Path
    application: Application
    letter_check: LetterCheck
    transitions: list[TransitionRecord]


def _write(path: Path, content: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_text(json.dumps(content, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def known_fingerprints(root: Path, past_applications: list[dict[str, Any]]) -> dict[str, str]:
    """Fingerprint → where it was seen (existing dossier or past application)."""

    seen: dict[str, str] = {}
    for meta in root.glob("*/meta.json"):
        data = json.loads(meta.read_text(encoding="utf-8"))
        seen[data["fingerprint"]] = f"dossier {meta.parent.name}"
    for entry in past_applications:
        if entry.get("company") and entry.get("position"):
            fp = offer_fingerprint(entry["company"], entry["position"], entry.get("location") or "")
            seen.setdefault(fp, f"ancienne candidature {entry.get('folder')}")
    return seen


def create_dossier(
    *,
    offer_text: str,
    verified: VerifiedExtraction,
    evaluation: OfferEvaluation,
    memory: MemorySnapshot,
    root: Path,
    as_of: date,
    past_applications: list[dict[str, Any]] | None = None,
    include_salary: bool = False,
    run_id: UUID | None = None,
    url: str | None = None,
) -> DossierResult:
    extraction = verified.extraction
    decision = evaluation.assessment.decision
    if decision == MatchDecision.REJECT:
        raise RejectedOfferError("Offre rejetée par l'évaluation : aucun dossier préparé")
    company, title, location = extraction.company or "", extraction.title or "", extraction.location or ""
    if not company or not title:
        raise BewerbungspilotError("Entreprise ou intitulé inconnu : dossier impossible sans identification sûre")
    fingerprint = offer_fingerprint(company, title, location)
    past = past_applications or []
    seen = known_fingerprints(root, past)
    if fingerprint in seen:
        raise DuplicateOfferError(f"Offre déjà traitée : {seen[fingerprint]}")

    # Keep enough room for the generated document names on Windows, where deeply nested
    # temporary and workspace paths may still be subject to the legacy path limit.
    folder = root / f"{as_of:%Y-%m-%d}_{slug(company, 24)}_{slug(title, 30)}"
    rules = ProfileRules(memory, as_of=as_of)
    packet = build_context(memory, TaskType.COVER_LETTER, as_of=as_of,
                           query=" ".join(extraction.technologies_required + extraction.technologies_mentioned + [title]))
    letter = build_letter(memory, evaluation, extraction, offer_text, as_of=as_of, include_salary=include_salary)
    name = rules.fact("identity.display_name")
    check = check_letter(
        letter.text, memory=memory, rules=rules, offer_text=offer_text, company=company,
        allowed_numbers_text=letter.allowed_numbers_text,
        other_companies=[e.get("company") for e in past if e.get("company")],
        signature=str(name.value) if name else None,
    )
    cv_plan = build_cv_plan(memory, evaluation, offer_text)

    # Application state: DISCOVERED -> EVALUATED -> SELECTED / AWAITING_USER.
    now = datetime.now(timezone.utc)
    machine = ApplicationStateMachine()
    application = Application(job_offer_id=evaluation.assessment.job_offer_id, local_folder=folder, created_at=now, updated_at=now)
    records: list[TransitionRecord] = []
    reasons = [i.statement for i in evaluation.assessment.unknowns + evaluation.assessment.gaps]
    if not check.ok:
        reasons.append("lettre refusée par le contrôle : " + "; ".join(i.rule for i in check.issues))
    target = S.SELECTED if decision == MatchDecision.APPLY and check.ok else S.AWAITING_USER
    ctx = TransitionContext(assessment=evaluation.assessment, awaiting_reason=("; ".join(reasons) or "validation humaine")[:1000])
    for step in (S.EVALUATED, target):
        application, record = machine.apply(application, TransitionCommand(
            application_id=application.id, expected_state=application.state, expected_version=application.state_version,
            target_state=step, reason=f"préparation du dossier ({decision.value})", actor=TransitionActor.SYSTEM,
            requested_at=now, run_id=run_id, context=ctx))
        records.append(record)

    _write(folder / "Stellenanzeige.md", f"# {title} — {company}\n\n{offer_text.strip()}\n")
    _write(folder / "extraction.json", {"extraction": extraction.model_dump(), "removed": verified.removed})
    _write(folder / "evaluation.json", evaluation.model_dump(mode="json"))
    _write(folder / "fact_pack.json", {"fact_ids": [str(i) for i in packet.fact_ids], "sources": packet.sources,
                                       "facts_used_by_letter": letter.facts_used})
    _write(folder / "Motivationsschreiben.md", letter.text)
    _write(folder / "letter_check.json", check.model_dump())
    _write(folder / "cv_plan.json", cv_plan.model_dump())
    _write(folder / "application.json", application.model_dump(mode="json"))
    _write(folder / "transitions.json", [r.model_dump(mode="json") for r in records])
    _write(folder / "meta.json", {"fingerprint": fingerprint, "company": company, "title": title, "location": location, "url": url,
                                  "created_at": now.isoformat(), "run_id": str(run_id) if run_id else None})
    _write(folder / "README.md", _readme(extraction, evaluation, check, application, as_of, url))
    refresh_readme(folder)
    return DossierResult(folder=folder, application=application, letter_check=check, transitions=records)


def _readme(extraction, evaluation: OfferEvaluation, check: LetterCheck, application: Application, as_of: date,
            url: str | None = None) -> str:
    a = evaluation.assessment
    status = {S.SELECTED: "prête à relire (documents PDF à générer)", S.AWAITING_USER: "en attente de ta décision"}[application.state]
    lines = [
        f"# Candidature — {extraction.company} — {extraction.title}",
        "",
        f"- Entreprise : {extraction.company}",
        f"- Poste : {extraction.title}",
        f"- Lieu : {extraction.location or 'inconnu'}",
        f"- Lien officiel : {url or 'à compléter'}",
        f"- Date de publication affichée : {extraction.publication_date or 'non indiquée'}",
        f"- Date de préparation : {as_of:%d.%m.%Y}",
        f"- Statut : {status}",
        f"- Décision du moteur : {a.decision.value} ({a.score}/100)",
        f"- Correspondance principale : {'; '.join(i.statement for i in a.matches[:6])}",
        f"- Lacunes ou risques : {'; '.join(i.statement for i in (a.gaps + a.unknowns)) or 'aucun'}",
        f"- Projets recommandés : {', '.join(a.recommended_projects) or 'aucun'}",
        f"- Contrôle de la lettre : {'OK' if check.ok else 'REFUSÉE — ' + '; '.join(i.rule + ' ' + i.detail for i in check.issues)}",
        "- CV envoyé : non (PDF à générer, étape 4.3)",
        "- Lettre envoyée : non",
        "- Confirmation de réception : aucune",
        "- Prochaine action : relire la lettre, générer les PDF, décision humaine avant tout envoi",
        "",
    ]
    return "\n".join(lines)
