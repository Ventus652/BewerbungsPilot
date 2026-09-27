"""Fill, verify, submit once, recover without re-clicking (phase 4.5).

* ``fill_application``: DOCUMENTS_PREPARED → FORM_IN_PROGRESS → (read-back verified) →
  READY_TO_SUBMIT, or AWAITING_USER with the list of questions.
* ``authorize_submission``: the user's single-use authorization for one execution.
* ``submit_application``: one click per authorization; the attempt is recorded **before**
  clicking; confirmed → SUBMITTED → CONFIRMED with the portal receipt; unknown → FAILED
  (OUTCOME_UNKNOWN).
* ``recover_submission``: checks the portal status — never clicks again.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel

from bewerbungspilot.api.control import RunControl
from bewerbungspilot.core.errors import BewerbungspilotError
from bewerbungspilot.domain.enums import (
    ActionResult,
    ApplicationState as S,
    FailureKind,
    ReceiptKind,
    TransitionActor,
)
from bewerbungspilot.domain.models import ActionEvidence, ActionLog, Application, DocumentArtifact, FormAnswer, SubmissionReceipt
from bewerbungspilot.domain.state_machine import (
    ApplicationStateMachine,
    FailureInfo,
    SubmissionAuthorization,
    TransitionCommand,
    TransitionContext,
    TransitionRecord,
)
from bewerbungspilot.documents.dossier_readme import refresh_readme
from bewerbungspilot.documents.render import DOCUMENT_POLICY
from bewerbungspilot.memory.parsing import slug
from bewerbungspilot.memory.records import MemorySnapshot

from .form_filler import AnswerAction, FillPlan, plan_answers
from .portal import PortalAdapter, PortalError


class SubmissionRefused(BewerbungspilotError):
    """Submission impossible: wrong state, no authorization, or authorization already used."""


class FlowResult(BaseModel):
    state: S
    transitions: list[TransitionRecord]
    questions: list[str] = []
    detail: dict[str, Any] = {}


def _read(folder: Path, name: str, default: Any) -> Any:
    path = folder / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _write(folder: Path, name: str, data: Any) -> None:
    (folder / name).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


class _Dossier:
    def __init__(self, folder: Path, run_id: UUID | None) -> None:
        self.folder, self.run_id = folder, run_id
        self.application = Application.model_validate_json((folder / "application.json").read_text(encoding="utf-8"))
        self.documents = [DocumentArtifact.model_validate(d) for d in _read(folder, "documents.json", {}).get("documents", [])]
        self.meta = _read(folder, "meta.json", {})
        self.machine = ApplicationStateMachine(DOCUMENT_POLICY)
        self.records: list[TransitionRecord] = []

    def move(self, target: S, reason: str, *, actor: TransitionActor = TransitionActor.SYSTEM,
             execution_id: UUID | None = None, **context: Any) -> None:
        now = max(datetime.now(timezone.utc), self.application.updated_at)
        auth = context.get("submission_authorization")
        if auth is not None:
            now = max(now, auth.authorized_at)
        command = TransitionCommand(
            **({"execution_id": execution_id} if execution_id else {}),
            application_id=self.application.id, expected_state=self.application.state,
            expected_version=self.application.state_version, target_state=target, reason=reason, actor=actor,
            requested_at=now, run_id=self.run_id, context=TransitionContext(**context))
        self.application, record = self.machine.apply(self.application, command)
        self.records.append(record)
        self.save()

    def set_answers(self, answers: dict[str, FormAnswer]) -> None:
        data = self.application.model_dump()
        data["form_answers"] = answers
        self.application = Application.model_validate(data)
        self.save()

    def save(self) -> None:
        (self.folder / "application.json").write_text(self.application.model_dump_json(indent=2), encoding="utf-8")
        transitions = _read(self.folder, "transitions.json", [])
        known = {t["execution_id"] for t in transitions}
        transitions += [r.model_dump(mode="json") for r in self.records if str(r.execution_id) not in known]
        _write(self.folder, "transitions.json", transitions)
        refresh_readme(self.folder)


def fill_application(folder: Path, portal: PortalAdapter, *, memory: MemorySnapshot, as_of,
                     control: RunControl | None = None, run_id: UUID | None = None) -> FlowResult:
    dossier = _Dossier(folder, run_id)
    if dossier.application.state == S.DOCUMENTS_PREPARED:
        dossier.move(S.FORM_IN_PROGRESS, f"ouverture du formulaire {portal.name}")
    elif dossier.application.state != S.FORM_IN_PROGRESS:
        raise BewerbungspilotError(f"Remplissage impossible depuis l'état {dossier.application.state}")

    snapshot = portal.observe()
    plan: FillPlan = plan_answers(snapshot, memory=memory, as_of=as_of, documents=dossier.documents, folder=folder,
                                  job_title=dossier.meta.get("title", ""), user_answers=_read(folder, "user_answers.json", {}))
    _write(folder, "form_plan.json", plan.model_dump())
    for answer in plan.answers:
        if control is not None:
            control.checkpoint()  # between two fields, never in the middle of one
        if answer.action == AnswerAction.FILL:
            portal.fill(answer.field_id, answer.value or "")
        elif answer.action == AnswerAction.UPLOAD:
            portal.upload(answer.field_id, folder / (answer.path or ""))

    observed = portal.read_back()
    _write(folder, "form_evidence.json", {"portal": portal.name, "read_back": observed,
                                           "at": datetime.now(timezone.utc).isoformat()})
    answers: dict[str, FormAnswer] = {}
    mismatches = []
    for answer in plan.answers:
        if answer.action not in (AnswerAction.FILL, AnswerAction.UPLOAD):
            continue
        expected = answer.value if answer.action == AnswerAction.FILL else Path(answer.path or "").name
        verified = observed.get(answer.field_id) == expected
        if not verified:
            mismatches.append(f"{answer.label} : attendu {expected!r}, relu {observed.get(answer.field_id)!r}")
        answers[slug(answer.field_id, 60)] = FormAnswer(value=expected, source_fact_keys=answer.fact_keys, verified=verified,
                                                         sensitive=any(k.startswith("legal.") for k in answer.fact_keys))
    dossier.set_answers(answers)

    # Safety net: a required field still empty after filling is never "ready" (27/09/2026 live
    # Personio form: required markers not detected, READY_TO_SUBMIT with three empty fields).
    asked = {q.field_id for q in plan.questions}
    empty_required = [f"{f.label} — champ obligatoire resté vide" for f in snapshot.fields
                      if f.required and f.id not in asked and not observed.get(f.id)]
    questions = [f"{q.label} — {q.reason}" for q in plan.questions] + mismatches + empty_required
    if questions:
        dossier.move(S.AWAITING_USER, "; ".join(questions)[:990], awaiting_reason="; ".join(questions)[:1000])
    else:
        dossier.move(S.READY_TO_SUBMIT, "formulaire rempli et relu", documents=dossier.documents, blocking_unknowns=[])
    return FlowResult(state=dossier.application.state, transitions=dossier.records, questions=questions,
                      detail={"filled": len(answers), "portal": portal.name})


def restore_form(folder: Path, portal: PortalAdapter, *, memory: MemorySnapshot, as_of) -> FlowResult:
    """Fill a fresh page for an application already READY_TO_SUBMIT, without changing its state.

    The answers must be exactly those the user validated; any difference (changed memory,
    changed form) refuses the submission instead of sending something new.
    """

    dossier = _Dossier(folder, None)
    if dossier.application.state != S.READY_TO_SUBMIT:
        raise SubmissionRefused(f"Restauration impossible dans l'état {dossier.application.state}")
    snapshot = portal.observe()
    plan = plan_answers(snapshot, memory=memory, as_of=as_of, documents=dossier.documents, folder=folder,
                        job_title=dossier.meta.get("title", ""), user_answers=_read(folder, "user_answers.json", {}))
    if plan.questions:
        raise SubmissionRefused("Le formulaire pose de nouvelles questions : " + "; ".join(q.label for q in plan.questions))
    expected = {k: a.value for k, a in dossier.application.form_answers.items()}
    differences = []
    for answer in plan.answers:
        if answer.action not in (AnswerAction.FILL, AnswerAction.UPLOAD):
            continue
        value = answer.value if answer.action == AnswerAction.FILL else Path(answer.path or "").name
        if expected.get(slug(answer.field_id, 60)) != value:
            differences.append(answer.label)
    if differences or len([a for a in plan.answers if a.action in (AnswerAction.FILL, AnswerAction.UPLOAD)]) != len(expected):
        raise SubmissionRefused("Réponses différentes de celles validées : " + ", ".join(differences or ["nombre de champs"]))
    for answer in plan.answers:
        if answer.action == AnswerAction.FILL:
            portal.fill(answer.field_id, answer.value or "")
        elif answer.action == AnswerAction.UPLOAD:
            portal.upload(answer.field_id, folder / (answer.path or ""))
    observed = portal.read_back()
    for answer in plan.answers:
        if answer.action in (AnswerAction.FILL, AnswerAction.UPLOAD):
            wanted = answer.value if answer.action == AnswerAction.FILL else Path(answer.path or "").name
            if observed.get(answer.field_id) != wanted:
                raise SubmissionRefused(f"Relecture différente pour « {answer.label} »")
    return FlowResult(state=dossier.application.state, transitions=[], detail={"restored": len(expected)})


def reopen_form(folder: Path, reason: str, *, run_id: UUID | None = None) -> FlowResult:
    """READY_TO_SUBMIT → FORM_IN_PROGRESS when the live form no longer matches the validated answers.

    Moves away from submission: any pending authorization is voided (it was bound to the old
    state version anyway) and the form must be filled, read back and validated again.
    """

    dossier = _Dossier(folder, run_id)
    if dossier.application.state != S.READY_TO_SUBMIT:
        raise BewerbungspilotError(f"Réouverture impossible depuis l'état {dossier.application.state}")
    dossier.move(S.FORM_IN_PROGRESS, f"formulaire à refaire : {reason}"[:990])
    auth_path = folder / "submission_authorization.json"
    if auth_path.exists():
        data = json.loads(auth_path.read_text(encoding="utf-8"))
        if not data.get("used"):
            data.update(used=True, voided=f"formulaire rouvert : {reason}"[:300])
            _write(folder, "submission_authorization.json", data)
    return FlowResult(state=dossier.application.state, transitions=dossier.records)


def authorize_submission(folder: Path, *, by: str = "user") -> dict[str, Any]:
    application = Application.model_validate_json((folder / "application.json").read_text(encoding="utf-8"))
    if application.state != S.READY_TO_SUBMIT:
        raise SubmissionRefused(f"Autorisation impossible dans l'état {application.state}")
    authorization = {"application_id": str(application.id), "execution_id": str(uuid4()), "authorized_by": by,
                     "authorized_at": datetime.now(timezone.utc).isoformat(), "used": False}
    _write(folder, "submission_authorization.json", authorization)
    return authorization


def submit_application(folder: Path, portal: PortalAdapter, *, run_id: UUID | None = None) -> FlowResult:
    dossier = _Dossier(folder, run_id)
    if dossier.application.state != S.READY_TO_SUBMIT:
        raise SubmissionRefused(f"Envoi impossible dans l'état {dossier.application.state}")
    raw = _read(folder, "submission_authorization.json", None)
    if not raw or raw.get("used"):
        raise SubmissionRefused("Aucune autorisation utilisateur valide (une autorisation ne sert qu'une fois)")
    authorization = SubmissionAuthorization(
        application_id=UUID(raw["application_id"]), execution_id=UUID(raw["execution_id"]),
        authorized_by=raw["authorized_by"], authorized_at=datetime.fromisoformat(raw["authorized_at"]))
    attempts = _read(folder, "submission_attempts.json", [])
    if any(a["execution_id"] == raw["execution_id"] for a in attempts):
        raise SubmissionRefused("Cette autorisation a déjà servi à un clic : jamais de second clic automatique")

    # Record the non-idempotent attempt BEFORE clicking.
    clicked_at = datetime.now(timezone.utc)
    log = ActionLog(run_id=run_id or uuid4(), action="submit_application", target=portal.name,
                    redacted_input={"application_id": str(dossier.application.id)}, result=ActionResult.AWAITING_USER,
                    idempotent=False, safe_to_retry=False)
    attempts.append({"execution_id": raw["execution_id"], "clicked_at": clicked_at.isoformat(), "log": log.model_dump(mode="json")})
    _write(folder, "submission_attempts.json", attempts)
    raw["used"] = True
    _write(folder, "submission_authorization.json", raw)

    try:
        outcome = portal.submit()
    except PortalError as exc:
        dossier.move(S.FAILED, "issue de l'envoi inconnue après le clic",
                     failure=FailureInfo(kind=FailureKind.OUTCOME_UNKNOWN, reason=str(exc)))
        return FlowResult(state=dossier.application.state, transitions=dossier.records, detail={"error": str(exc)})
    if not outcome.confirmed:
        dossier.move(S.FAILED, "le portail a refusé l'envoi",
                     failure=FailureInfo(kind=FailureKind.PERMANENT, reason=outcome.message))
        return FlowResult(state=dossier.application.state, transitions=dossier.records, detail={"message": outcome.message})
    dossier.move(S.SUBMITTED, "envoi autorisé et effectué une fois", execution_id=authorization.execution_id,
                 submission_authorization=authorization, submitted_at=clicked_at)
    receipt = SubmissionReceipt(kind=ReceiptKind.PORTAL, reference=f"{outcome.reference} — {outcome.message}"[:1000])
    dossier.move(S.CONFIRMED, "accusé de réception visible", receipt=receipt)
    return FlowResult(state=dossier.application.state, transitions=dossier.records,
                      detail={"reference": outcome.reference, "message": outcome.message})


def recover_submission(folder: Path, portal: PortalAdapter, *, run_id: UUID | None = None) -> FlowResult:
    """After an unknown outcome: look, never click."""

    dossier = _Dossier(folder, run_id)
    if dossier.application.state != S.FAILED or dossier.application.failure_kind != FailureKind.OUTCOME_UNKNOWN:
        raise BewerbungspilotError("Aucune issue inconnue à vérifier")
    attempts = _read(folder, "submission_attempts.json", [])
    status = portal.check_status()
    if status.confirmed and status.reference:
        clicked_at = datetime.fromisoformat(attempts[-1]["clicked_at"]) if attempts else datetime.now(timezone.utc)
        dossier.move(S.SUBMITTED, "envoi constaté dans le portail (aucun nouveau clic)",
                     submission_evidence=ActionEvidence(kind="portal_status", reference=f"{status.reference} — {status.message}"),
                     submitted_at=clicked_at)
        dossier.move(S.CONFIRMED, "preuve de réception constatée",
                     receipt=SubmissionReceipt(kind=ReceiptKind.PORTAL, reference=f"{status.reference} — {status.message}"))
    else:
        dossier.move(S.AWAITING_USER, "issue inconnue : vérifier le portail et la boîte mail avant toute nouvelle tentative",
                     awaiting_reason="Envoi incertain : aucune trace dans le portail. Vérifier la boîte mail ; "
                                     "une nouvelle tentative exigera une nouvelle autorisation.")
    return FlowResult(state=dossier.application.state, transitions=dossier.records, detail={"status": status.message})
