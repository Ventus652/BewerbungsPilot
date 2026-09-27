"""Phase 2.5 — every allowed arc, forbidden arcs and non-idempotent safety."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from bewerbungspilot.core.errors import (
    DuplicateTransitionError,
    ForbiddenTransitionError,
    PreconditionFailedError,
    StaleStateError,
)
from bewerbungspilot.domain import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    ActionEvidence,
    Application,
    ApplicationState as S,
    ApplicationStateMachine,
    AssessmentItem,
    DocumentArtifact,
    DocumentType,
    FailureInfo,
    FailureKind,
    FormAnswer,
    MatchAssessment,
    MatchDecision,
    ReceiptKind,
    SubmissionAuthorization,
    SubmissionReceipt,
    TransitionActor,
    TransitionCommand,
    TransitionContext,
    VisualValidationStatus,
)

T0 = datetime(2026, 9, 26, 18, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=5)
APP_ID = UUID("00000000-0000-0000-0000-00000000a001")
OFFER_ID = UUID("00000000-0000-0000-0000-00000000b001")
MACHINE = ApplicationStateMachine()


def cv(application_id: UUID = APP_ID, visual=VisualValidationStatus.PASSED) -> DocumentArtifact:
    return DocumentArtifact(
        id=UUID("00000000-0000-0000-0000-00000000c001") if application_id == APP_ID else uuid4(),
        application_id=application_id,
        document_type=DocumentType.CV,
        path=Path("data/generated/acme/cv.pdf"),
        sha256="b" * 64,
        version="v1",
        generator_source="template:cv:v1",
        visual_validation=visual,
        created_at=T0,
    )


def assessment(decision=MatchDecision.APPLY, offer_id: UUID = OFFER_ID) -> MatchAssessment:
    gaps = [] if decision == MatchDecision.APPLY else [
        AssessmentItem(statement="German level unknown", source_refs=["offer:req:de"])
    ]
    return MatchAssessment(
        id=UUID("00000000-0000-0000-0000-00000000d001"),
        job_offer_id=offer_id,
        score=70 if decision != MatchDecision.REJECT else 20,
        decision=decision,
        matches=[AssessmentItem(statement="Python", source_refs=["offer:req:python"])],
        gaps=gaps,
        prompt_version="p1",
        model_used="gpt-oss:20b",
        assessed_at=T0,
    )


ASSESSMENT = assessment()
CV = cv()


def build(state: S, **extra) -> Application:
    """Test fixture: construct an application directly in a given state."""
    data = dict(
        id=APP_ID,
        job_offer_id=OFFER_ID,
        state=state,
        state_version=3,
        local_folder=Path("data/private/applications/acme"),
        document_ids=[CV.id],
        form_answers={"first_name": FormAnswer(value="Test", verified=True)},
        created_at=T0,
        updated_at=T0,
        assessment_id=ASSESSMENT.id,
    )
    if state in (S.SUBMITTED, S.CONFIRMED):
        data["submitted_at"] = T0
    if state == S.CONFIRMED:
        data["receipt"] = SubmissionReceipt(kind=ReceiptKind.EMAIL, reference="msg-1", captured_at=T0)
    if state == S.AWAITING_USER:
        data.update(awaiting_reason="check salary question", resume_state=S.FORM_IN_PROGRESS)
    if state == S.FAILED:
        data.update(
            failed_from=S.FORM_IN_PROGRESS,
            failure_kind=FailureKind.RETRYABLE_IDEMPOTENT,
            failure_reason="page timeout before any input",
        )
    data.update(extra)
    return Application.model_validate(data)


def command(app: Application, target: S, *, actor=TransitionActor.SYSTEM, **ctx) -> TransitionCommand:
    execution_id = ctx.pop("execution_id", uuid4())
    return TransitionCommand(
        execution_id=execution_id,
        application_id=app.id,
        expected_state=app.state,
        expected_version=app.state_version,
        target_state=target,
        reason=f"test {app.state}->{target}",
        actor=actor,
        requested_at=T1,
        context=TransitionContext(**ctx),
    )


def valid_case(source: S, target: S) -> tuple[Application, TransitionCommand]:
    """Smallest complete evidence set for each allowed arc."""
    extra: dict = {}
    ctx: dict = {}
    actor = TransitionActor.SYSTEM
    if source == S.AWAITING_USER:
        actor = TransitionActor.USER
        if target != S.WITHDRAWN:
            extra["resume_state"] = target
    if source == S.FAILED:
        if target == S.SUBMITTED:
            extra.update(failed_from=S.READY_TO_SUBMIT, failure_kind=FailureKind.OUTCOME_UNKNOWN)
            ctx.update(
                submitted_at=T1,
                submission_evidence=ActionEvidence(kind="portal", reference="status page shows 'eingereicht'"),
            )
        elif target not in (S.AWAITING_USER, S.WITHDRAWN):
            extra["failed_from"] = target
    if target in (S.EVALUATED, S.SELECTED, S.REJECTED):
        ctx["assessment"] = assessment(MatchDecision.REJECT) if target == S.REJECTED else ASSESSMENT
    if target in (S.DOCUMENTS_PREPARED, S.READY_TO_SUBMIT):
        ctx["documents"] = [CV]
    if target == S.SUBMITTED and source == S.READY_TO_SUBMIT:
        execution_id = uuid4()
        ctx.update(
            execution_id=execution_id,
            submitted_at=T1,
            submission_authorization=SubmissionAuthorization(
                application_id=APP_ID, execution_id=execution_id, authorized_by="user", authorized_at=T1
            ),
        )
    if target == S.CONFIRMED:
        ctx["receipt"] = SubmissionReceipt(kind=ReceiptKind.PAGE, reference="Danke für Ihre Bewerbung", captured_at=T1)
    if target == S.AWAITING_USER:
        ctx["awaiting_reason"] = "user decision needed"
    if target == S.FAILED:
        kind = FailureKind.OUTCOME_UNKNOWN if source in (S.READY_TO_SUBMIT, S.SUBMITTED) else FailureKind.RETRYABLE_IDEMPOTENT
        ctx["failure"] = FailureInfo(kind=kind, reason="simulated")
    if target == S.WITHDRAWN:
        actor = TransitionActor.USER
    if target == S.REJECTED:
        extra["assessment_id"] = None
    app = build(source, **extra)
    return app, command(app, target, actor=actor, **ctx)


ALL_ARCS = [(s, t) for s, targets in ALLOWED_TRANSITIONS.items() for t in sorted(targets)]
FORBIDDEN_ARCS = [
    (s, t) for s in S for t in S if t not in ALLOWED_TRANSITIONS[s] and s not in TERMINAL_STATES
]


def test_table_covers_all_twelve_states_and_terminals() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(S) and len(S) == 12
    assert TERMINAL_STATES == {S.REJECTED, S.CONFIRMED, S.WITHDRAWN}
    assert len(ALL_ARCS) == 38


@pytest.mark.parametrize(("source", "target"), ALL_ARCS, ids=[f"{s}->{t}" for s, t in ALL_ARCS])
def test_every_allowed_arc(source: S, target: S) -> None:
    app, cmd = valid_case(source, target)
    before = app.model_dump()
    new_app, record = MACHINE.apply(app, cmd)
    assert new_app.state == target
    assert new_app.state_version == app.state_version + 1
    assert new_app.last_transition_id == cmd.execution_id
    assert (record.from_state, record.to_state) == (source, target)
    assert record.version_after == record.version_before + 1
    assert app.model_dump() == before  # the original object is never mutated


@pytest.mark.parametrize(("source", "target"), FORBIDDEN_ARCS, ids=[f"{s}->{t}" for s, t in FORBIDDEN_ARCS])
def test_forbidden_arcs(source: S, target: S) -> None:
    app = build(source)
    with pytest.raises(ForbiddenTransitionError):
        MACHINE.apply(app, command(app, target, actor=TransitionActor.USER))


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES))
def test_terminal_states_have_no_exit(terminal: S) -> None:
    app = build(terminal)
    for target in S:
        with pytest.raises(ForbiddenTransitionError):
            MACHINE.apply(app, command(app, target, actor=TransitionActor.USER))


def test_state_cannot_be_assigned_directly() -> None:
    app = build(S.DISCOVERED)
    with pytest.raises(Exception):
        app.state = S.CONFIRMED  # type: ignore[misc]
    assert app.state == S.DISCOVERED


def test_stale_expected_state_is_refused() -> None:
    app, cmd = valid_case(S.DISCOVERED, S.EVALUATED)
    stale = cmd.model_copy(update={"expected_state": S.SELECTED})
    with pytest.raises(StaleStateError):
        MACHINE.apply(app, stale)


def test_stale_version_is_refused() -> None:
    app, cmd = valid_case(S.DISCOVERED, S.EVALUATED)
    with pytest.raises(StaleStateError):
        MACHINE.apply(app, cmd.model_copy(update={"expected_version": app.state_version - 1}))


def test_command_for_another_application_is_refused() -> None:
    app, cmd = valid_case(S.DISCOVERED, S.EVALUATED)
    with pytest.raises(StaleStateError):
        MACHINE.apply(app, cmd.model_copy(update={"application_id": uuid4()}))


def test_same_execution_is_never_applied_twice() -> None:
    app, cmd = valid_case(S.READY_TO_SUBMIT, S.SUBMITTED)
    submitted, _ = MACHINE.apply(app, cmd)
    with pytest.raises(DuplicateTransitionError):
        MACHINE.apply(submitted, cmd)


def test_confirmation_without_receipt_is_refused_and_state_kept() -> None:
    app = build(S.SUBMITTED)
    with pytest.raises(PreconditionFailedError):
        MACHINE.apply(app, command(app, S.CONFIRMED))
    assert app.state == S.SUBMITTED and app.receipt is None


def test_submission_without_authorization_is_refused() -> None:
    app = build(S.READY_TO_SUBMIT)
    with pytest.raises(PreconditionFailedError):
        MACHINE.apply(app, command(app, S.SUBMITTED, submitted_at=T1))


def test_authorization_for_another_execution_is_refused() -> None:
    app = build(S.READY_TO_SUBMIT)
    auth = SubmissionAuthorization(application_id=APP_ID, execution_id=uuid4(), authorized_by="user", authorized_at=T1)
    with pytest.raises(PreconditionFailedError):
        MACHINE.apply(app, command(app, S.SUBMITTED, submitted_at=T1, submission_authorization=auth))


def test_authorization_given_before_current_state_is_refused() -> None:
    app = build(S.READY_TO_SUBMIT, updated_at=T1)
    execution_id = uuid4()
    auth = SubmissionAuthorization(application_id=APP_ID, execution_id=execution_id, authorized_by="user", authorized_at=T0)
    with pytest.raises(PreconditionFailedError):
        MACHINE.apply(app, command(app, S.SUBMITTED, execution_id=execution_id, submitted_at=T1, submission_authorization=auth))


def test_submission_failure_cannot_be_marked_retryable() -> None:
    app = build(S.READY_TO_SUBMIT)
    failure = FailureInfo(kind=FailureKind.RETRYABLE_IDEMPOTENT, reason="click timeout")
    with pytest.raises(PreconditionFailedError):
        MACHINE.apply(app, command(app, S.FAILED, failure=failure))


def test_unknown_submission_outcome_never_returns_to_ready_automatically() -> None:
    app = build(S.READY_TO_SUBMIT)
    failure = FailureInfo(kind=FailureKind.OUTCOME_UNKNOWN, reason="interrupted after click")
    failed, _ = MACHINE.apply(app, command(app, S.FAILED, failure=failure))
    assert failed.failed_from == S.READY_TO_SUBMIT
    with pytest.raises(ForbiddenTransitionError):
        MACHINE.apply(failed, command(failed, S.READY_TO_SUBMIT))
    with pytest.raises(PreconditionFailedError):  # no observed evidence -> no SUBMITTED either
        MACHINE.apply(failed, command(failed, S.SUBMITTED, submitted_at=T1))
    paused, _ = MACHINE.apply(failed, command(failed, S.AWAITING_USER, awaiting_reason="verify portal and inbox"))
    assert paused.resume_state == S.READY_TO_SUBMIT
    with pytest.raises(PreconditionFailedError):  # the system cannot resume on its own
        MACHINE.apply(paused, command(paused, S.READY_TO_SUBMIT, documents=[CV]))


def test_non_retryable_failure_cannot_be_retried() -> None:
    app = build(S.FAILED, failure_kind=FailureKind.PERMANENT)
    with pytest.raises(PreconditionFailedError):
        MACHINE.apply(app, command(app, S.FORM_IN_PROGRESS))


def test_idempotent_retry_only_returns_to_origin() -> None:
    app = build(S.FAILED)  # failed_from FORM_IN_PROGRESS
    with pytest.raises(PreconditionFailedError):
        MACHINE.apply(app, command(app, S.SELECTED))
    retried, _ = MACHINE.apply(app, command(app, S.FORM_IN_PROGRESS))
    assert retried.failure_kind is None and retried.failed_from is None


def test_documents_of_another_application_are_refused() -> None:
    app = build(S.SELECTED)
    with pytest.raises(PreconditionFailedError):
        MACHINE.apply(app, command(app, S.DOCUMENTS_PREPARED, documents=[cv(application_id=uuid4())]))


def test_documents_need_acceptable_visual_validation() -> None:
    app = build(S.SELECTED)
    pending = cv(visual=VisualValidationStatus.PENDING)
    with pytest.raises(PreconditionFailedError):
        MACHINE.apply(app, command(app, S.DOCUMENTS_PREPARED, documents=[pending]))


def test_missing_required_document_type_is_refused() -> None:
    app = build(S.SELECTED)
    letter = cv().model_copy(update={"document_type": DocumentType.COVER_LETTER})
    with pytest.raises(PreconditionFailedError):
        MACHINE.apply(app, command(app, S.DOCUMENTS_PREPARED, documents=[letter]))


def test_ready_to_submit_requires_verified_answers_and_no_blocking_unknown() -> None:
    unverified = build(S.FORM_IN_PROGRESS, form_answers={"salary": FormAnswer(value="?", verified=False)})
    with pytest.raises(PreconditionFailedError):
        MACHINE.apply(unverified, command(unverified, S.READY_TO_SUBMIT, documents=[CV]))
    app = build(S.FORM_IN_PROGRESS)
    with pytest.raises(PreconditionFailedError):
        MACHINE.apply(app, command(app, S.READY_TO_SUBMIT, documents=[CV], blocking_unknowns=["work permit question"]))


def test_evaluation_requires_assessment_of_same_offer() -> None:
    app = build(S.DISCOVERED, assessment_id=None)
    with pytest.raises(PreconditionFailedError):
        MACHINE.apply(app, command(app, S.EVALUATED))
    with pytest.raises(PreconditionFailedError):
        MACHINE.apply(app, command(app, S.EVALUATED, assessment=assessment(offer_id=uuid4())))
    evaluated, _ = MACHINE.apply(app, command(app, S.EVALUATED, assessment=ASSESSMENT))
    assert evaluated.assessment_id == ASSESSMENT.id


def test_system_cannot_select_a_review_decision_but_user_can() -> None:
    app = build(S.EVALUATED)
    review = assessment(MatchDecision.REVIEW)
    with pytest.raises(PreconditionFailedError):
        MACHINE.apply(app, command(app, S.SELECTED, assessment=review))
    selected, _ = MACHINE.apply(app, command(app, S.SELECTED, actor=TransitionActor.USER, assessment=review))
    assert selected.state == S.SELECTED


def test_awaiting_user_resumes_only_to_recorded_state() -> None:
    app = build(S.FORM_IN_PROGRESS)
    paused, _ = MACHINE.apply(app, command(app, S.AWAITING_USER, awaiting_reason="salary expectation"))
    assert paused.resume_state == S.FORM_IN_PROGRESS
    with pytest.raises(PreconditionFailedError):
        MACHINE.apply(paused, command(paused, S.SELECTED, actor=TransitionActor.USER))
    resumed, _ = MACHINE.apply(paused, command(paused, S.FORM_IN_PROGRESS, actor=TransitionActor.USER))
    assert resumed.awaiting_reason is None and resumed.resume_state is None


def test_awaiting_user_requires_reason() -> None:
    app = build(S.EVALUATED)
    with pytest.raises(PreconditionFailedError):
        MACHINE.apply(app, command(app, S.AWAITING_USER))


def test_only_user_can_withdraw() -> None:
    app = build(S.SELECTED)
    with pytest.raises(PreconditionFailedError):
        MACHINE.apply(app, command(app, S.WITHDRAWN))


def test_full_happy_path_keeps_a_record_per_step() -> None:
    app = build(S.DISCOVERED, assessment_id=None, state_version=0)
    path = [S.EVALUATED, S.SELECTED, S.DOCUMENTS_PREPARED, S.FORM_IN_PROGRESS, S.READY_TO_SUBMIT, S.SUBMITTED, S.CONFIRMED]
    records = []
    for target in path:
        _, cmd = valid_case(app.state, target)
        cmd = cmd.model_copy(update={"expected_version": app.state_version})
        app, record = MACHINE.apply(app, cmd)
        records.append(record)
    assert app.state == S.CONFIRMED and app.state_version == len(path)
    assert [r.to_state for r in records] == path
    assert app.receipt is not None and app.submitted_at is not None


def test_model_rejects_awaiting_user_without_resume_state() -> None:
    with pytest.raises(Exception):
        build(S.AWAITING_USER, resume_state=None)


def test_model_rejects_retryable_submission_failure() -> None:
    with pytest.raises(Exception):
        build(S.FAILED, failed_from=S.READY_TO_SUBMIT)
