"""Read dossiers and apply the user's decisions through the state machine (phase 4.4)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from bewerbungspilot.domain.enums import ApplicationState as S, TransitionActor
from bewerbungspilot.domain.models import Application, DocumentArtifact
from bewerbungspilot.domain.state_machine import ApplicationStateMachine, TransitionCommand, TransitionContext
from bewerbungspilot.documents.render import DOCUMENT_POLICY

USER_ACTIONS = ("resume", "withdraw", "authorize", "select")


def _json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def summarize(folder: Path) -> dict[str, Any]:
    meta = _json(folder / "meta.json", {})
    application = _json(folder / "application.json", {})
    evaluation = _json(folder / "evaluation.json", {}).get("assessment", {})
    documents = _json(folder / "documents.json", {}).get("documents", [])
    return {
        "name": folder.name,
        "company": meta.get("company"),
        "title": meta.get("title"),
        "location": meta.get("location"),
        "state": application.get("state"),
        "awaiting_reason": application.get("awaiting_reason"),
        "resume_state": application.get("resume_state"),
        "decision": evaluation.get("decision"),
        "score": evaluation.get("score"),
        "matches": [i["statement"] for i in evaluation.get("matches", [])][:6],
        "risks": [i["statement"] for i in evaluation.get("gaps", []) + evaluation.get("unknowns", [])][:6],
        "projects": evaluation.get("recommended_projects", []),
        "documents": [{"type": d["document_type"], "file": d["path"], "valid": d["visual_validation"] == "PASSED"} for d in documents],
        "files": sorted(p.name for p in folder.iterdir() if p.suffix in (".pdf", ".md")),
        "created_at": meta.get("created_at"),
        "questions": [
            {"field_id": a["field_id"], "label": a["label"], "reason": a["reason"], "options": a.get("options", []),
             "answer": (_json(folder / "user_answers.json", {}) or {}).get(a["field_id"])}
            for a in (_json(folder / "form_plan.json", {}) or {}).get("answers", []) if a["action"] == "ASK"
        ],
        "authorized": bool((_json(folder / "submission_authorization.json", {}) or {}).get("execution_id"))
        and not (_json(folder / "submission_authorization.json", {}) or {}).get("used"),
    }


def list_dossiers(root: Path) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    folders = [p for p in root.iterdir() if p.is_dir() and (p / "meta.json").exists()]
    return sorted((summarize(f) for f in folders), key=lambda d: d.get("created_at") or "", reverse=True)


def apply_user_action(folder: Path, action: str, *, execution_id: UUID | None = None) -> dict[str, Any]:
    """The user resumes a paused dossier (to its recorded state only) or withdraws it."""

    if action not in USER_ACTIONS:
        raise ValueError(f"Action inconnue : {action}")
    if action == "select":
        return select_despite_review(folder)
    if action == "authorize":
        from bewerbungspilot.browser.runner import authorize_submission

        authorization = authorize_submission(folder, by="user (interface)")
        return {"state": "READY_TO_SUBMIT", "authorization": {k: authorization[k] for k in ("execution_id", "authorized_at")}}
    application = Application.model_validate_json((folder / "application.json").read_text(encoding="utf-8"))
    target = application.resume_state if action == "resume" else S.WITHDRAWN
    if target is None:
        raise ValueError("Aucun état de reprise enregistré pour ce dossier")
    documents = [DocumentArtifact.model_validate(d) for d in _json(folder / "documents.json", {}).get("documents", [])]
    evaluation = _json(folder / "evaluation.json", {}).get("assessment")
    context: dict[str, Any] = {"documents": documents}
    if evaluation and target in (S.EVALUATED, S.SELECTED):
        from bewerbungspilot.domain.models import MatchAssessment
        context["assessment"] = MatchAssessment.model_validate(evaluation)
    command = TransitionCommand(
        application_id=application.id, expected_state=application.state, expected_version=application.state_version,
        target_state=target, reason=f"décision utilisateur via l'interface : {action}", actor=TransitionActor.USER,
        requested_at=max(datetime.now(timezone.utc), application.updated_at), context=TransitionContext(**context),
        **({"execution_id": execution_id} if execution_id else {}),
    )
    new_application, record = ApplicationStateMachine(DOCUMENT_POLICY).apply(application, command)
    (folder / "application.json").write_text(new_application.model_dump_json(indent=2), encoding="utf-8")
    transitions = _json(folder / "transitions.json", [])
    transitions.append(record.model_dump(mode="json"))
    (folder / "transitions.json").write_text(json.dumps(transitions, ensure_ascii=False, indent=2), encoding="utf-8")
    from bewerbungspilot.documents.dossier_readme import refresh_readme

    refresh_readme(folder)
    return {"state": new_application.state.value, "transition": record.model_dump(mode="json")}


def save_user_answers(folder: Path, answers: dict[str, str]) -> dict[str, str]:
    """Store the user's answers to the form questions of this dossier (validated against the plan)."""

    plan = _json(folder / "form_plan.json", {}) or {}
    asked = {a["field_id"]: a for a in plan.get("answers", []) if a["action"] == "ASK"}
    clean: dict[str, str] = {}
    for field_id, value in answers.items():
        if field_id not in asked:
            raise ValueError(f"Question inconnue : {field_id}")
        value = str(value).strip()
        options = asked[field_id].get("options") or []
        if not value or (options and value not in options and asked[field_id].get("kind") != "checkbox"):
            raise ValueError(f"Réponse invalide pour « {asked[field_id]['label']} »")
        clean[field_id] = value
    stored = {**(_json(folder / "user_answers.json", {}) or {}), **clean}
    (folder / "user_answers.json").write_text(json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8")
    return stored


def select_despite_review(folder: Path) -> dict[str, Any]:
    """The user decides to apply to an offer the engine marked REVIEW.

    AWAITING_USER → EVALUATED → SELECTED, both by the USER, then CV and letter are rendered.
    """

    from datetime import date

    from bewerbungspilot.documents.render import render_documents
    from bewerbungspilot.domain.models import MatchAssessment
    from bewerbungspilot.memory.store import MemoryStore

    application = Application.model_validate_json((folder / "application.json").read_text(encoding="utf-8"))
    if application.state == S.AWAITING_USER and application.resume_state == S.EVALUATED:
        apply_user_action(folder, "resume")
        application = Application.model_validate_json((folder / "application.json").read_text(encoding="utf-8"))
    if application.state != S.EVALUATED:
        raise ValueError(f"« Candidater quand même » impossible dans l'état {application.state}")
    assessment = MatchAssessment.model_validate(_json(folder / "evaluation.json", {})["assessment"])
    command = TransitionCommand(
        application_id=application.id, expected_state=S.EVALUATED, expected_version=application.state_version,
        target_state=S.SELECTED, reason="l'utilisateur choisit de candidater malgré les points à vérifier",
        actor=TransitionActor.USER, requested_at=max(datetime.now(timezone.utc), application.updated_at),
        context=TransitionContext(assessment=assessment))
    application, record = ApplicationStateMachine(DOCUMENT_POLICY).apply(application, command)
    (folder / "application.json").write_text(application.model_dump_json(indent=2), encoding="utf-8")
    transitions = _json(folder / "transitions.json", [])
    transitions.append(record.model_dump(mode="json"))
    (folder / "transitions.json").write_text(json.dumps(transitions, ensure_ascii=False, indent=2), encoding="utf-8")
    app_root = folder.parents[3]
    store = MemoryStore(app_root / "data" / "private")
    index = store.root / "applications_index.json"
    past = [a.get("company") for a in json.loads(index.read_text(encoding="utf-8"))["applications"]] if index.exists() else []
    rendered = render_documents(folder, memory=store.load(), library=store.load_cv_library(), as_of=date.today(), past_companies=past)
    return {"state": rendered.application.state.value, "cv_ok": rendered.cv.ok, "letter_ok": rendered.letter.ok}
