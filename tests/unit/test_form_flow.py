"""Phase 4.5 — fictional form end to end: fill, verify, authorize, submit once, recover."""

import json
from datetime import date
from pathlib import Path

import pytest

from bewerbungspilot.api.control import RunControl, RunState, StopRequested
from bewerbungspilot.api.dossiers import apply_user_action
from bewerbungspilot.browser.form_filler import AnswerAction, plan_answers
from bewerbungspilot.browser.portal import DEMO_FIELDS, FakePortal, FieldKind, FormField, FormSnapshot
from bewerbungspilot.browser.runner import (
    SubmissionRefused,
    authorize_submission,
    fill_application,
    recover_submission,
    reopen_form,
    submit_application,
)
from bewerbungspilot.documents.dossier import create_dossier
from bewerbungspilot.documents.render import render_documents
from bewerbungspilot.domain import ApplicationState as S
from bewerbungspilot.domain.enums import FactCategory, FailureKind
from bewerbungspilot.jobs.evaluation import evaluate_offer
from bewerbungspilot.jobs.extraction import OfferExtraction, verify_extraction
from bewerbungspilot.memory.benchmark import _fact, memory_from_benchmark_profile
from bewerbungspilot.memory.cv_library import build_library
from bewerbungspilot.memory.importer import import_sources
from bewerbungspilot.memory.records import MemorySnapshot
from bewerbungspilot.memory.store import MemoryStore

BENCH = Path(__file__).resolve().parents[2] / "benchmarks"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "memory_root"
TODAY = date(2026, 9, 27)
MEMORY = MemorySnapshot(facts=[*memory_from_benchmark_profile().facts,
                               _fact("identity.display_name", "Alex Beispiel", FactCategory.IDENTITY),
                               _fact("contact.email", "alex.beispiel@example.org", FactCategory.CONTACT),
                               _fact("education.institution", "Hochschule Musterstadt", FactCategory.EDUCATION),
                               _fact("education.bachelor_expected_end", "2027-09-30", FactCategory.EDUCATION)])
LIBRARY = build_library(FIXTURES)
USER_ANSWERS = {"vorname": "Alex", "nachname": "Beispiel", "arbeitserlaubnis": "Ja", "datenschutz": "akzeptiert"}


def prepared_dossier(root: Path) -> Path:
    text = json.loads((BENCH / "cases" / "A01.json").read_text(encoding="utf-8"))["input"]
    extraction = OfferExtraction.model_validate(json.loads((BENCH / "expected_reviewed" / "A01.json").read_text(encoding="utf-8")))
    verified = verify_extraction(extraction, text)
    evaluation = evaluate_offer(verified, text, MEMORY, as_of=TODAY)
    dossier = create_dossier(offer_text=text, verified=verified, evaluation=evaluation, memory=MEMORY, root=root, as_of=TODAY)
    assert render_documents(dossier.folder, memory=MEMORY, library=LIBRARY, as_of=TODAY).application.state == S.DOCUMENTS_PREPARED
    return dossier.folder


def ready_dossier(root: Path, portal: FakePortal) -> Path:
    folder = prepared_dossier(root)
    first = fill_application(folder, portal, memory=MEMORY, as_of=TODAY)
    assert first.state == S.AWAITING_USER
    (folder / "user_answers.json").write_text(json.dumps(USER_ANSWERS), encoding="utf-8")
    assert apply_user_action(folder, "resume")["state"] == S.FORM_IN_PROGRESS.value
    assert fill_application(folder, portal, memory=MEMORY, as_of=TODAY).state == S.READY_TO_SUBMIT
    return folder


def test_first_pass_asks_only_what_the_memory_cannot_answer(tmp_path) -> None:
    folder = prepared_dossier(tmp_path)
    result = fill_application(folder, FakePortal(), memory=MEMORY, as_of=TODAY)
    asked = " | ".join(result.questions)
    for label in ("Vorname", "Nachname", "Arbeitserlaubnis", "Datenschutz"):
        assert label in asked
    plan = json.loads((folder / "form_plan.json").read_text(encoding="utf-8"))
    filled = {a["field_id"]: a["value"] for a in plan["answers"] if a["action"] == "FILL"}
    assert filled["wochenstunden"] == "20" and filled["eintritt"] == "15.10.2026" and filled["email"] == "alex.beispiel@example.org"
    uploads = {a["field_id"] for a in plan["answers"] if a["action"] == "UPLOAD"}
    assert uploads == {"lebenslauf", "anschreiben"}


def test_full_flow_submits_exactly_once_with_receipt(tmp_path) -> None:
    portal = FakePortal()
    folder = ready_dossier(tmp_path, portal)
    application = json.loads((folder / "application.json").read_text(encoding="utf-8"))
    assert all(a["verified"] for a in application["form_answers"].values())
    with pytest.raises(SubmissionRefused):
        submit_application(folder, portal)  # no authorization yet
    authorize_submission(folder)
    result = submit_application(folder, portal)
    assert result.state == S.CONFIRMED and portal.submissions == 1
    assert json.loads((folder / "application.json").read_text(encoding="utf-8"))["receipt"]["kind"] == "PORTAL"
    with pytest.raises(SubmissionRefused):
        submit_application(folder, portal)
    assert portal.submissions == 1


def test_crash_after_click_is_recovered_by_looking_not_clicking(tmp_path) -> None:
    portal = FakePortal(crash_on_submit=True)
    folder = ready_dossier(tmp_path, portal)
    authorize_submission(folder)
    assert submit_application(folder, portal).state == S.FAILED
    application = json.loads((folder / "application.json").read_text(encoding="utf-8"))
    assert application["failure_kind"] == FailureKind.OUTCOME_UNKNOWN.value
    with pytest.raises(SubmissionRefused):
        submit_application(folder, portal)
    assert recover_submission(folder, portal).state == S.CONFIRMED
    assert portal.submissions == 1


def test_unfound_submission_goes_back_to_the_user_and_needs_new_authorization(tmp_path) -> None:
    portal = FakePortal(crash_on_submit=True)
    folder = ready_dossier(tmp_path, portal)
    authorize_submission(folder)
    submit_application(folder, portal)
    portal.received_reference = None  # the portal shows nothing
    assert recover_submission(folder, portal).state == S.AWAITING_USER
    assert apply_user_action(folder, "resume")["state"] == S.READY_TO_SUBMIT.value
    with pytest.raises(SubmissionRefused):
        submit_application(folder, portal)  # the old authorization was consumed by the first click
    portal.crash_on_submit = False
    authorize_submission(folder)
    assert submit_application(folder, portal).state == S.CONFIRMED
    assert portal.submissions == 2  # the second click came from a new, explicit human authorization


def test_portal_refusal_is_a_permanent_failure(tmp_path) -> None:
    portal = FakePortal()
    folder = ready_dossier(tmp_path, portal)
    portal.values["lebenslauf"] = None  # e.g. the upload silently disappeared
    authorize_submission(folder)
    result = submit_application(folder, portal)
    assert result.state == S.FAILED and "Pflichtfelder" in result.detail["message"]


def test_stop_between_fields_leaves_no_half_action(tmp_path) -> None:
    folder = prepared_dossier(tmp_path)
    control = RunControl(tmp_path / "control.json")
    control.set(RunState.STOPPED)
    portal = FakePortal()
    with pytest.raises(StopRequested):
        fill_application(folder, portal, memory=MEMORY, as_of=TODAY, control=control)
    assert all(v is None for v in portal.values.values())
    assert json.loads((folder / "application.json").read_text(encoding="utf-8"))["state"] == S.FORM_IN_PROGRESS.value


def test_invalid_user_answer_is_asked_again(tmp_path) -> None:
    snapshot = FormSnapshot(portal="t", title="t", fields=[f for f in DEMO_FIELDS if f.id == "arbeitserlaubnis"])
    plan = plan_answers(snapshot, memory=MEMORY, as_of=TODAY, documents=[], folder=tmp_path, job_title="x",
                        user_answers={"arbeitserlaubnis": "Vielleicht"})
    assert plan.answers[0].action == AnswerAction.ASK


def test_work_permit_rule_with_a_documented_expiry(tmp_path) -> None:
    """Fictional profile: permit valid until 31.03.2027, renewal not confirmed."""
    result = import_sources(FIXTURES)
    store = MemoryStore(tmp_path)
    memory, _ = store.ingest(MemorySnapshot(), result.facts, result.sources, run_id=__import__("uuid").uuid4(), kind="t")

    def plan_for(label: str):
        field = FormField(id="q", label=label, kind=FieldKind.RADIO, required=True, options=["Ja", "Nein"])
        return plan_answers(FormSnapshot(portal="t", title="t", fields=[field]), memory=memory, as_of=TODAY,
                            documents=[], folder=tmp_path, job_title="Werkstudent Software").answers[0]

    assert plan_for("Ist Ihre Arbeitserlaubnis bis 2027 gültig?").action == AnswerAction.ASK
    covered = plan_for("Ist Ihre Arbeitserlaubnis bis 01.12.2026 gültig?")
    assert covered.action == AnswerAction.FILL and covered.value == "Ja"


def test_unknown_required_field_is_never_guessed(tmp_path) -> None:
    field = FormField(id="x", label="Lieblingsfarbe", kind=FieldKind.TEXT, required=True)
    optional = FormField(id="y", label="Hobbys", kind=FieldKind.TEXT)
    plan = plan_answers(FormSnapshot(portal="t", title="t", fields=[field, optional]), memory=MEMORY, as_of=TODAY,
                        documents=[], folder=tmp_path, job_title="x")
    assert [a.action for a in plan.answers] == [AnswerAction.ASK, AnswerAction.SKIP]


def test_user_answers_are_validated_and_authorization_goes_through_the_api(tmp_path) -> None:
    from bewerbungspilot.api.dossiers import list_dossiers, save_user_answers
    portal = FakePortal()
    folder = prepared_dossier(tmp_path)
    fill_application(folder, portal, memory=MEMORY, as_of=TODAY)
    (summary,) = list_dossiers(tmp_path)
    assert {q["field_id"] for q in summary["questions"]} == {"vorname", "nachname", "arbeitserlaubnis", "datenschutz"}
    with pytest.raises(ValueError):
        save_user_answers(folder, {"arbeitserlaubnis": "Vielleicht"})
    with pytest.raises(ValueError):
        save_user_answers(folder, {"email": "x@example.org"})  # not a question: never overridable from here
    save_user_answers(folder, USER_ANSWERS)
    apply_user_action(folder, "resume")
    assert fill_application(folder, portal, memory=MEMORY, as_of=TODAY).state == S.READY_TO_SUBMIT
    assert apply_user_action(folder, "authorize")["authorization"]["execution_id"]
    assert list_dossiers(tmp_path)[0]["authorized"] is True
    assert submit_application(folder, portal).state == S.CONFIRMED


def test_changed_form_reopens_and_voids_the_authorization(tmp_path) -> None:
    """27/09/2026: a READY_TO_SUBMIT dossier whose live form changed must be refilled, never sent."""
    portal = FakePortal()
    folder = ready_dossier(tmp_path, portal)
    authorize_submission(folder)
    result = reopen_form(folder, "nouveau champ obligatoire")
    assert result.state == S.FORM_IN_PROGRESS
    assert json.loads((folder / "submission_authorization.json").read_text(encoding="utf-8"))["used"] is True
    with pytest.raises(SubmissionRefused):
        submit_application(folder, portal)
    assert portal.submissions == 0
    assert fill_application(folder, FakePortal(), memory=MEMORY, as_of=TODAY).state == S.READY_TO_SUBMIT
    with pytest.raises(SubmissionRefused):
        submit_application(folder, portal)  # the old authorization cannot be reused
    assert portal.submissions == 0


def test_dossier_card_follows_every_step(tmp_path) -> None:
    """Regression: a dossier README must reflect a confirmed submission."""
    portal = FakePortal()
    folder = ready_dossier(tmp_path, portal)
    card = (folder / "README.md").read_text(encoding="utf-8")
    assert "rien n'est envoyé sans ton « oui »" in card and "## Formulaire" in card
    authorize_submission(folder)
    submit_application(folder, portal)
    card = (folder / "README.md").read_text(encoding="utf-8")
    assert "réception confirmée" in card and "Date de candidature" in card and "Confirmation du portail" in card
    assert "en attente" not in card.split("## Suivi")[0]
