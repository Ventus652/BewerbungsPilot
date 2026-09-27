"""Render and verify the PDFs of a dossier, then move the application to DOCUMENTS_PREPARED.

Nothing is sent. A document gets ``visual_validation=PASSED`` only when every automatic
check passes (one page, text extractable, required terms, order of projects, no other
company, fonts embedded); a human still reviews before any submission.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel

from bewerbungspilot.domain.enums import ApplicationState as S, DocumentType, TransitionActor, VisualValidationStatus
from bewerbungspilot.domain.models import Application, DocumentArtifact
from bewerbungspilot.domain.state_machine import (
    ApplicationStateMachine,
    TransitionCommand,
    TransitionContext,
    TransitionPolicy,
    TransitionRecord,
)
from bewerbungspilot.memory.cv_library import CvDocument, strip_markup
from bewerbungspilot.memory.parsing import slug
from bewerbungspilot.memory.records import MemorySnapshot
from bewerbungspilot.memory.rules import ProfileRules

from .dossier_readme import refresh_readme
from .checks import LetterCheck, check_letter, undocumented_technologies
from .pdf import LetterHeader, PdfCheck, choose_base_cv, render_cv, render_letter, reorder_projects, sha256_file, verify_pdf

DOCUMENT_POLICY = TransitionPolicy(required_document_types=frozenset({DocumentType.CV, DocumentType.COVER_LETTER}))


class RenderResult(BaseModel):
    base_cv: str
    cv: PdfCheck
    letter: PdfCheck
    letter_check: LetterCheck
    documents: list[DocumentArtifact]
    application: Application
    transition: TransitionRecord | None = None
    excluded_cvs: dict[str, list[str]]


def cv_truth_issues(document: CvDocument, memory: MemorySnapshot, rules: ProfileRules) -> list[str]:
    text = "\n".join(strip_markup(e.text) for e in document.header)
    text += "\n" + "\n".join(strip_markup(e.text) for s in document.sections for e in s.entries)
    issues = [i.detail.split(" :")[0] for i in undocumented_technologies(text, memory=memory, rules=rules)]
    issues += [f"{v.rule}:{v.excerpt}" for v in rules.check_text(text) if v.rule != "expertise_claim"]
    return sorted(set(issues))


def _file_label(value: str, limit: int = 40) -> str:
    """``Pixelpfad AG`` → ``Pixelpfad_AG``; keeps the original casing, ASCII only."""

    import unicodedata

    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    label = re.sub(r"[^A-Za-z0-9]+", "_", ascii_value).strip("_")
    return label[:limit].rstrip("_")


def render_documents(
    folder: Path,
    *,
    memory: MemorySnapshot,
    library: list[CvDocument],
    as_of: date,
    past_companies: list[str] | None = None,
    include_address: bool = False,
    run_id: UUID | None = None,
) -> RenderResult:
    rules = ProfileRules(memory, as_of=as_of)
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    from bewerbungspilot.jobs.evaluation import OfferEvaluation

    from .cv_plan import build_cv_plan

    evaluation = OfferEvaluation.model_validate_json((folder / "evaluation.json").read_text(encoding="utf-8"))
    offer_text = (folder / "Stellenanzeige.md").read_text(encoding="utf-8")
    # The plan is rebuilt from the current memory so a corrected profile is always reflected.
    plan = build_cv_plan(memory, evaluation, offer_text).model_dump()
    (folder / "cv_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    application = Application.model_validate_json((folder / "application.json").read_text(encoding="utf-8"))
    letter_md = (folder / "Motivationsschreiben.md").read_text(encoding="utf-8")
    company, title = meta["company"], meta["title"]
    others = [c for c in (past_companies or []) if c and slug(c)[:8] != slug(company)[:8]]

    # 1. The letter may have been edited by hand: check it again before rendering.
    name_fact = rules.fact("identity.display_name")
    name = str(name_fact.value) if name_fact else ""
    numbers = " ".join(str(f.value) for f in memory.facts if f.key.startswith(("availability.", "education.", "compensation.")))
    from .letters import MONTHS_DE, MONTHS_EN  # local import keeps module graph simple
    for key in ("availability.start_date", "education.bachelor_expected_end"):
        fact = rules.fact(key)
        if fact:
            d = date.fromisoformat(str(fact.value))
            numbers += f" {d.day}. {MONTHS_DE[d.month - 1]} {d.year} {MONTHS_EN[d.month - 1]} {d.day}, {d.year}"
    letter_check = check_letter(letter_md, memory=memory, rules=rules, offer_text=offer_text, company=company,
                                allowed_numbers_text=numbers + " " + title, other_companies=others, signature=name)

    # 2. CV: closest validated CV without unsupported claims, projects reordered.
    excluded = {d.source: issues for d in library if (issues := cv_truth_issues(d, memory, rules))}
    clean = [d for d in library if d.source not in excluded]
    from bewerbungspilot.memory.context import query_terms

    terms = query_terms(" ".join(plan.get("skills_highlighted", [])) + " " + title)
    if terms & {"frontend", "react", "web", "typescript"}:
        terms |= {"web", "webentwicklung", "responsiv", "frontend"}
    if terms & {"data", "ml", "ai", "ki", "python"}:
        terms |= {"data", "machine learning", "künstliche intelligenz", "ai", "python"}
    base = choose_base_cv(clean, plan["language"], terms)
    cv_document = reorder_projects(base, plan["project_order"])
    doc_label = "Lebenslauf" if plan["language"] == "de" else "CV"
    letter_label = "Motivationsschreiben" if plan["language"] == "de" else "Cover_Letter"
    from .letters import clean_title

    # Conservative filename budgets keep PDF generation reliable inside deep Windows
    # workspaces and pytest temporary directories.
    stem = _file_label(name, 20)
    target = f"{_file_label(company, 16)}_{_file_label(clean_title(title), 24)}"
    cv_path = folder / f"{stem}_{doc_label}_{target}.pdf"
    letter_path = folder / f"{stem}_{letter_label}_{target}.pdf"
    # Remove only the PDFs this module produced before (listed in documents.json), never other files.
    previous = folder / "documents.json"
    if previous.exists():
        for old_doc in json.loads(previous.read_text(encoding="utf-8")).get("documents", []):
            old_path = folder / old_doc["path"]
            if old_path.suffix == ".pdf" and old_path not in (cv_path, letter_path) and old_path.exists():
                old_path.unlink()
    render_cv(cv_document, cv_path, title=f"{name} | {doc_label}", subject=f"{title} – {company}")

    city_fact = rules.fact("contact.city")
    city = str(city_fact.value).split(",")[0].strip() if city_fact else ""
    email, phone = rules.fact("contact.email"), rules.fact("contact.phone_international")
    address = rules.disclose("contact.postal_address", explicitly_requested=True) if include_address else None
    location_city = re.split(r"[(/,]", meta.get("location") or "")[0].strip()
    if location_city.isupper():
        location_city = location_city.title()  # "DARMSTADT" as printed by some portals
    header = LetterHeader(
        name=name, city=city, email=str(email.value) if email else None, phone=str(phone.value) if phone else None,
        address=str(address.value) if address is not None and address.value else None,
        recipient=[company] + ([location_city] if location_city else []), language=plan["language"], on=as_of,
    )
    render_letter(letter_md, header, letter_path, title=f"{name} | {letter_label} {company}")

    previews = folder / "_previews"
    if previews.exists():  # previews are ours only; stale images from older names are dropped
        for stale in previews.glob("*.png"):
            stale.unlink()
    first_projects = [p for p in plan["project_order"][:2] if p.casefold() in cv_document.plain_text().casefold()]
    cv_check = verify_pdf(cv_path, must_contain=[name] + first_projects[:1], ordered=first_projects,
                          must_not_contain=others, preview_dir=previews)
    letter_pdf_check = verify_pdf(letter_path, must_contain=[name, company], must_not_contain=others, preview_dir=previews)
    if not letter_check.ok:
        letter_pdf_check.issues.append("contenu refusé : " + "; ".join(i.rule for i in letter_check.issues))
        letter_pdf_check.ok = False

    documents = []
    for doc_type, path, check, source in (
        (DocumentType.CV, cv_path, cv_check, f"cv_library:{base.source}"),
        (DocumentType.COVER_LETTER, letter_path, letter_pdf_check, "documents/letters.py"),
    ):
        documents.append(DocumentArtifact(
            application_id=application.id, document_type=doc_type, path=Path(path.name), sha256=sha256_file(path),
            version="v1", generator_source=source,
            visual_validation=VisualValidationStatus.PASSED if check.ok else VisualValidationStatus.FAILED,
        ))

    transition = None
    if application.state == S.SELECTED and cv_check.ok and letter_pdf_check.ok:
        machine = ApplicationStateMachine(DOCUMENT_POLICY)
        now = datetime.now(timezone.utc)
        application, transition = machine.apply(application, TransitionCommand(
            application_id=application.id, expected_state=S.SELECTED, expected_version=application.state_version,
            target_state=S.DOCUMENTS_PREPARED, reason="CV et lettre rendus et vérifiés", actor=TransitionActor.SYSTEM,
            requested_at=max(now, application.updated_at), run_id=run_id, context=TransitionContext(documents=documents)))
        transitions = json.loads((folder / "transitions.json").read_text(encoding="utf-8"))
        transitions.append(transition.model_dump(mode="json"))
        (folder / "transitions.json").write_text(json.dumps(transitions, ensure_ascii=False, indent=2), encoding="utf-8")
    elif application.state == S.DOCUMENTS_PREPARED and transition is None:
        # Re-render of an already prepared dossier: the state stays, the document set is replaced
        # so that READY_TO_SUBMIT later checks the documents that really exist.
        if cv_check.ok and letter_pdf_check.ok:
            data = application.model_dump()
            data.update(document_ids=[d.id for d in documents], updated_at=max(datetime.now(timezone.utc), application.updated_at))
            application = Application.model_validate(data)
    (folder / "application.json").write_text(application.model_dump_json(indent=2), encoding="utf-8")
    (folder / "documents.json").write_text(json.dumps({
        "base_cv": base.source, "excluded_cvs": excluded,
        "documents": [d.model_dump(mode="json") for d in documents],
        "checks": {"cv": cv_check.model_dump(), "letter": letter_pdf_check.model_dump(), "letter_content": letter_check.model_dump()},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    refresh_readme(folder)
    return RenderResult(base_cv=base.source, cv=cv_check, letter=letter_pdf_check, letter_check=letter_check,
                        documents=documents, application=application, transition=transition, excluded_cvs=excluded)
