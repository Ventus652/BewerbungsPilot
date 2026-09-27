"""Single authority for application state transitions (phase 2.5).

Design rules:

* one transition table (``ALLOWED_TRANSITIONS``); nothing else may change ``Application.state``;
* a transition is a pure function: the original object is never mutated and a new, fully
  re-validated ``Application`` is returned together with a journalisable record;
* the caller states the state and version it believes current; a mismatch is refused;
* preconditions come from a typed ``TransitionContext`` — the machine never reads the disk;
* a non-idempotent step (submission) is never repeated automatically: a submission failure
  is always ``OUTCOME_UNKNOWN`` and only a user can bring the application back to
  ``READY_TO_SUBMIT``, through ``AWAITING_USER``.

Nothing here persists data; SQLite arrives in a later phase.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, ValidationError, model_validator

from ..core.errors import (
    DuplicateTransitionError,
    ForbiddenTransitionError,
    PreconditionFailedError,
    StaleStateError,
)
from .enums import (
    ApplicationState as S,
    DocumentType,
    FailureKind,
    MatchDecision,
    TransitionActor,
    VisualValidationStatus,
)
from .models import (
    ActionEvidence,
    Application,
    DocumentArtifact,
    DomainModel,
    MatchAssessment,
    SubmissionReceipt,
    utc_now,
)

ALLOWED_TRANSITIONS: Mapping[S, frozenset[S]] = MappingProxyType(
    {
        S.DISCOVERED: frozenset({S.EVALUATED, S.WITHDRAWN}),
        S.EVALUATED: frozenset({S.REJECTED, S.SELECTED, S.AWAITING_USER, S.WITHDRAWN}),
        S.SELECTED: frozenset({S.DOCUMENTS_PREPARED, S.AWAITING_USER, S.FAILED, S.WITHDRAWN}),
        S.DOCUMENTS_PREPARED: frozenset(
            {S.FORM_IN_PROGRESS, S.AWAITING_USER, S.FAILED, S.WITHDRAWN}
        ),
        S.FORM_IN_PROGRESS: frozenset(
            {S.READY_TO_SUBMIT, S.AWAITING_USER, S.FAILED, S.WITHDRAWN}
        ),
        S.AWAITING_USER: frozenset(
            {
                S.EVALUATED,
                S.SELECTED,
                S.DOCUMENTS_PREPARED,
                S.FORM_IN_PROGRESS,
                S.READY_TO_SUBMIT,
                S.WITHDRAWN,
            }
        ),
        # READY_TO_SUBMIT → FORM_IN_PROGRESS: the live form no longer matches what was validated
        # (27/09/2026): step back to refilling — away from submission, never towards it.
        S.READY_TO_SUBMIT: frozenset({S.SUBMITTED, S.FORM_IN_PROGRESS, S.AWAITING_USER, S.FAILED, S.WITHDRAWN}),
        S.SUBMITTED: frozenset({S.CONFIRMED, S.FAILED, S.WITHDRAWN}),
        S.FAILED: frozenset(
            {
                S.SELECTED,
                S.DOCUMENTS_PREPARED,
                S.FORM_IN_PROGRESS,
                S.AWAITING_USER,
                S.SUBMITTED,
                S.WITHDRAWN,
            }
        ),
        S.REJECTED: frozenset(),
        S.CONFIRMED: frozenset(),
        S.WITHDRAWN: frozenset(),
    }
)

TERMINAL_STATES: frozenset[S] = frozenset(
    state for state, targets in ALLOWED_TRANSITIONS.items() if not targets
)
SUBMISSION_STATES: frozenset[S] = frozenset({S.READY_TO_SUBMIT, S.SUBMITTED})
ACCEPTABLE_VISUAL_STATUSES = frozenset(
    {VisualValidationStatus.PASSED, VisualValidationStatus.NOT_APPLICABLE}
)


class TransitionPolicy(DomainModel):
    """Policy values mirrored from ``settings.safety``; defaults are the strict ones."""

    required_document_types: frozenset[DocumentType] = frozenset({DocumentType.CV})
    require_submission_authorization: bool = True


class SubmissionAuthorization(DomainModel):
    """Single-use user authorization bound to one application and one execution."""

    application_id: UUID
    execution_id: UUID
    authorized_by: str = Field(min_length=1, max_length=120)
    authorized_at: AwareDatetime


class FailureInfo(DomainModel):
    kind: FailureKind
    reason: str = Field(min_length=1, max_length=2000)


class TransitionContext(DomainModel):
    """Evidence supplied by the caller; the state machine does not fetch anything itself."""

    assessment: MatchAssessment | None = None
    documents: list[DocumentArtifact] = Field(default_factory=list)
    blocking_unknowns: list[str] = Field(default_factory=list)
    submission_authorization: SubmissionAuthorization | None = None
    submitted_at: AwareDatetime | None = None
    submission_evidence: ActionEvidence | None = None
    receipt: SubmissionReceipt | None = None
    awaiting_reason: str | None = Field(default=None, max_length=1000)
    failure: FailureInfo | None = None


class TransitionCommand(DomainModel):
    execution_id: UUID = Field(default_factory=uuid4)
    application_id: UUID
    expected_state: S
    expected_version: int = Field(ge=0)
    target_state: S
    reason: str = Field(min_length=1, max_length=1000)
    actor: TransitionActor
    requested_at: AwareDatetime = Field(default_factory=utc_now)
    run_id: UUID | None = None
    context: TransitionContext = Field(default_factory=TransitionContext)


class TransitionRecord(DomainModel):
    """Journalisable outcome of an applied transition (not persisted in this phase)."""

    execution_id: UUID
    application_id: UUID
    from_state: S
    to_state: S
    version_before: int = Field(ge=0)
    version_after: int = Field(ge=1)
    actor: TransitionActor
    reason: str
    occurred_at: AwareDatetime
    run_id: UUID | None = None
    checks: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def version_increments(self) -> "TransitionRecord":
        if self.version_after != self.version_before + 1:
            raise ValueError("version_after must be version_before + 1")
        return self


def allowed_targets(state: S) -> frozenset[S]:
    return ALLOWED_TRANSITIONS[state]


class ApplicationStateMachine:
    def __init__(self, policy: TransitionPolicy | None = None):
        self.policy = policy or TransitionPolicy()

    # ------------------------------------------------------------------ public API
    def apply(
        self, application: Application, command: TransitionCommand
    ) -> tuple[Application, TransitionRecord]:
        source, target = application.state, command.target_state
        self._check_identity_and_freshness(application, command)
        if target not in ALLOWED_TRANSITIONS[source]:
            raise ForbiddenTransitionError(
                f"Transition {source} -> {target} is not allowed",
                from_state=source,
                to_state=target,
            )

        updates: dict[str, object] = {}
        checks: list[str] = []
        self._check_source_rules(application, command, updates, checks)
        self._check_target_rules(application, command, updates, checks)

        # Leaving a pause or failure always clears its context.
        if source == S.AWAITING_USER and target != S.AWAITING_USER:
            updates.setdefault("awaiting_reason", None)
            updates.setdefault("resume_state", None)
        if source == S.FAILED and target != S.FAILED:
            updates.update(failed_from=None, failure_kind=None, failure_reason=None)

        data = application.model_dump()
        data.update(updates)
        data.update(
            state=target,
            state_version=application.state_version + 1,
            last_transition_id=command.execution_id,
            updated_at=command.requested_at,
        )
        try:
            new_application = Application.model_validate(data)
        except ValidationError as exc:
            raise PreconditionFailedError(
                f"Application invariants reject {source} -> {target}: {exc.errors()[0]['msg']}",
                from_state=source,
                to_state=target,
            ) from exc

        record = TransitionRecord(
            execution_id=command.execution_id,
            application_id=application.id,
            from_state=source,
            to_state=target,
            version_before=application.state_version,
            version_after=new_application.state_version,
            actor=command.actor,
            reason=command.reason,
            occurred_at=command.requested_at,
            run_id=command.run_id,
            checks=checks,
        )
        return new_application, record

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _fail(message: str, command: TransitionCommand, source: S) -> PreconditionFailedError:
        return PreconditionFailedError(message, from_state=source, to_state=command.target_state)

    def _check_identity_and_freshness(
        self, application: Application, command: TransitionCommand
    ) -> None:
        source = application.state
        if command.application_id != application.id:
            raise StaleStateError(
                "Command targets another application", from_state=source, to_state=command.target_state
            )
        if application.last_transition_id == command.execution_id:
            raise DuplicateTransitionError(
                f"Execution {command.execution_id} was already applied; it will not be repeated",
                from_state=source,
                to_state=command.target_state,
            )
        if command.expected_state != source or command.expected_version != application.state_version:
            raise StaleStateError(
                f"Expected {command.expected_state}@v{command.expected_version}, "
                f"found {source}@v{application.state_version}",
                from_state=source,
                to_state=command.target_state,
            )
        if command.requested_at < application.updated_at:
            raise StaleStateError(
                "Command is older than the application's last update",
                from_state=source,
                to_state=command.target_state,
            )

    def _check_source_rules(
        self,
        application: Application,
        command: TransitionCommand,
        updates: dict[str, object],
        checks: list[str],
    ) -> None:
        source, target = application.state, command.target_state
        if source == S.AWAITING_USER and target != S.WITHDRAWN:
            if command.actor != TransitionActor.USER:
                raise self._fail("Only the user can resume from AWAITING_USER", command, source)
            if target != application.resume_state:
                raise self._fail(
                    f"Resume target must be the recorded resume_state {application.resume_state}",
                    command,
                    source,
                )
            checks.append("resume_state_matches")
        if source == S.FAILED and target not in (S.AWAITING_USER, S.WITHDRAWN):
            if target == S.SUBMITTED:
                if application.failure_kind != FailureKind.OUTCOME_UNKNOWN:
                    raise self._fail("Only an unknown outcome can be resolved to SUBMITTED", command, source)
                if command.context.submission_evidence is None:
                    raise self._fail(
                        "Resolving an unknown outcome requires observed submission evidence, not a new click",
                        command,
                        source,
                    )
                checks.append("unknown_outcome_resolved_by_observation")
            else:
                if application.failure_kind != FailureKind.RETRYABLE_IDEMPOTENT:
                    raise self._fail(
                        f"Failure kind {application.failure_kind} is not automatically retryable",
                        command,
                        source,
                    )
                if target != application.failed_from:
                    raise self._fail(
                        f"An idempotent retry must return to {application.failed_from}", command, source
                    )
                checks.append("idempotent_retry")

    def _check_target_rules(
        self,
        application: Application,
        command: TransitionCommand,
        updates: dict[str, object],
        checks: list[str],
    ) -> None:
        source, target, ctx = application.state, command.target_state, command.context
        if target == S.EVALUATED:
            assessment = self._require_assessment(application, command, bind=source == S.DISCOVERED)
            updates["assessment_id"] = assessment.id
            checks.append("assessment_matches_offer")
        elif target == S.SELECTED:
            if source == S.EVALUATED:
                assessment = self._require_assessment(application, command, bind=False)
                if assessment.decision != MatchDecision.APPLY and command.actor != TransitionActor.USER:
                    raise self._fail("Only the user can select an offer not assessed as APPLY", command, source)
                checks.append("selection_decision_authorised")
        elif target == S.REJECTED:
            assessment = self._require_assessment(application, command, bind=False)
            if assessment.decision != MatchDecision.REJECT and command.actor != TransitionActor.USER:
                raise self._fail("Only the user can reject an offer not assessed as REJECT", command, source)
            checks.append("rejection_decision_authorised")
        elif target == S.DOCUMENTS_PREPARED:
            updates["document_ids"] = self._check_documents(application, command)
            checks.append("documents_owned_complete_and_visually_valid")
        elif target == S.READY_TO_SUBMIT:
            document_ids = self._check_documents(application, command)
            if set(document_ids) != set(application.document_ids):
                raise self._fail("Documents differ from the prepared set", command, source)
            self._check_form_answers(application, command)
            if ctx.blocking_unknowns:
                raise self._fail(f"Blocking unknowns remain: {ctx.blocking_unknowns}", command, source)
            checks += ["documents_unchanged", "answers_verified", "no_blocking_unknowns"]
        elif target == S.SUBMITTED:
            if source == S.READY_TO_SUBMIT:
                self._check_authorization(application, command)
                checks.append("submission_authorised")
            submitted_at = ctx.submitted_at or application.submitted_at
            if submitted_at is None:
                raise self._fail("SUBMITTED requires submitted_at", command, source)
            updates["submitted_at"] = submitted_at
        elif target == S.CONFIRMED:
            # The receipt rule itself lives in Application; it is re-validated, not duplicated.
            updates["receipt"] = ctx.receipt
            checks.append("receipt_validated_by_application_model")
        elif target == S.AWAITING_USER:
            if not ctx.awaiting_reason:
                raise self._fail("AWAITING_USER requires a reason", command, source)
            resume = application.failed_from if source == S.FAILED else source
            updates.update(awaiting_reason=ctx.awaiting_reason, resume_state=resume)
            checks.append(f"resume_state_recorded:{resume}")
        elif target == S.FAILED:
            if ctx.failure is None:
                raise self._fail("FAILED requires failure information", command, source)
            kind = ctx.failure.kind
            if source in SUBMISSION_STATES and kind == FailureKind.RETRYABLE_IDEMPOTENT:
                raise self._fail(
                    "A failure around submission is never retryable; record OUTCOME_UNKNOWN",
                    command,
                    source,
                )
            updates.update(failed_from=source, failure_kind=kind, failure_reason=ctx.failure.reason)
            checks.append(f"failure_classified:{kind}")
        elif target == S.WITHDRAWN:
            if command.actor != TransitionActor.USER:
                raise self._fail("Only the user can withdraw an application", command, source)
            checks.append("withdrawal_by_user")

    def _require_assessment(
        self, application: Application, command: TransitionCommand, *, bind: bool
    ) -> MatchAssessment:
        assessment = command.context.assessment
        source = application.state
        if assessment is None:
            raise self._fail("An assessment is required", command, source)
        if assessment.job_offer_id != application.job_offer_id:
            raise self._fail("Assessment belongs to another offer", command, source)
        if not bind and application.assessment_id not in (None, assessment.id):
            raise self._fail("Assessment differs from the one recorded on the application", command, source)
        return assessment

    def _check_documents(self, application: Application, command: TransitionCommand) -> list[UUID]:
        source = application.state
        documents = command.context.documents
        if not documents:
            raise self._fail("Documents are required", command, source)
        foreign = [str(d.id) for d in documents if d.application_id != application.id]
        if foreign:
            raise self._fail(f"Documents belong to another application: {foreign}", command, source)
        present = {d.document_type for d in documents}
        missing = self.policy.required_document_types - present
        if missing:
            raise self._fail(f"Missing required documents: {sorted(missing)}", command, source)
        invalid = [str(d.id) for d in documents if d.visual_validation not in ACCEPTABLE_VISUAL_STATUSES]
        if invalid:
            raise self._fail(f"Documents without acceptable visual validation: {invalid}", command, source)
        ids = [d.id for d in documents]
        if len(set(ids)) != len(ids):
            raise self._fail("Duplicate document identifiers", command, source)
        return ids

    def _check_form_answers(self, application: Application, command: TransitionCommand) -> None:
        unverified = [
            key
            for key, answer in application.form_answers.items()
            if not answer.verified or answer.value is None
        ]
        if unverified:
            raise self._fail(f"Unverified or empty form answers: {sorted(unverified)}", command, application.state)

    def _check_authorization(self, application: Application, command: TransitionCommand) -> None:
        if not self.policy.require_submission_authorization:
            return
        auth = command.context.submission_authorization
        source = application.state
        if auth is None:
            raise self._fail("Submission requires explicit user authorization", command, source)
        if auth.application_id != application.id or auth.execution_id != command.execution_id:
            raise self._fail("Authorization is bound to another application or execution", command, source)
        if auth.authorized_at > command.requested_at or auth.authorized_at < application.updated_at:
            raise self._fail("Authorization must be given for the current READY_TO_SUBMIT state", command, source)
