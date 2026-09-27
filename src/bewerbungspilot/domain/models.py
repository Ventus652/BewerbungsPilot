"""Validated business objects for one isolated application workflow."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    JsonValue,
    field_validator,
    model_validator,
)

from ..core.secrets import FORBIDDEN_SECRET_KEYS, contains_forbidden_key
from .enums import (
    ActionResult,
    ApplicationState,
    DisclosurePolicy,
    DocumentType,
    FactCategory,
    FailureKind,
    FactStatus,
    MatchDecision,
    PublicationDateReliability,
    ReceiptKind,
    RequirementLevel,
    RESTRICTED_POLICIES,
    SourceRank,
    VisualValidationStatus,
    normalize_policy,
)

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _assert_safe_path(path: Path) -> Path:
    if ".." in path.parts:
        raise ValueError("Path traversal components are forbidden")
    return path


_contains_forbidden_key = contains_forbidden_key  # backward-compatible alias


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class CandidateFact(DomainModel):
    """One sourced fact about the candidate.

    Status (is it true and current?) and sensitivity (may it circulate?) are separate:
    a fact can be CONFIRMED and sensitive. ``status=SENSITIVE`` is the phase 2.4 spelling
    of "confirmed and sensitive" and remains accepted.
    """

    id: UUID = Field(default_factory=uuid4)
    key: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9_.-]+$")
    value: JsonValue | date | AwareDatetime | None = None
    category: FactCategory
    status: FactStatus
    source: str | None = Field(default=None, max_length=500)
    validated_at: AwareDatetime | None = None
    disclosure_policy: DisclosurePolicy
    internal_comment: str | None = Field(default=None, max_length=2000)
    sensitive: bool = False
    source_rank: SourceRank | None = None
    source_ref: str | None = Field(default=None, max_length=500)
    valid_until: date | None = None
    needs_review: bool = False
    tags: list[str] = Field(default_factory=list)

    @property
    def is_sensitive(self) -> bool:
        return self.sensitive or self.status == FactStatus.SENSITIVE

    @property
    def effective_policy(self) -> DisclosurePolicy:
        return normalize_policy(self.disclosure_policy)

    def is_current(self, on: date) -> bool:
        """Usable as present truth: confirmed, not expired, not waiting for review."""

        if self.status not in (FactStatus.CONFIRMED, FactStatus.SENSITIVE) or self.needs_review:
            return False
        return self.valid_until is None or on <= self.valid_until

    @model_validator(mode="after")
    def validate_truth_and_disclosure(self) -> "CandidateFact":
        if self.status == FactStatus.UNKNOWN and self.value is not None:
            raise ValueError("UNKNOWN facts must not contain an asserted value")
        if self.status in (FactStatus.CONFIRMED, FactStatus.SENSITIVE):
            if self.value is None or not self.source or self.validated_at is None:
                raise ValueError("Confirmed or sensitive facts require value, source and validation date")
        if self.status == FactStatus.EXPIRED and (self.value is None or not self.source):
            raise ValueError("EXPIRED facts keep their historical value and source")
        if self.is_sensitive and self.disclosure_policy in (
            DisclosurePolicy.PUBLIC,
            DisclosurePolicy.APPLICATION_ONLY,
            DisclosurePolicy.PUBLIC_PROFILE,
            DisclosurePolicy.APPLICATION_STANDARD,
        ):
            raise ValueError("Sensitive facts require explicit-request-only or never-autofill disclosure")
        if self.source_rank == SourceRank.MODEL_INFERENCE and self.status in (
            FactStatus.CONFIRMED,
            FactStatus.SENSITIVE,
        ):
            raise ValueError("A model inference can never confirm a fact")
        if self.category == FactCategory.LEGAL_SENSITIVE and not self.is_sensitive:
            raise ValueError("LEGAL_SENSITIVE facts must be marked sensitive")
        return self


class JobRequirement(DomainModel):
    text: str = Field(min_length=1, max_length=1000)
    level: RequirementLevel
    evidence: str = Field(min_length=1, max_length=1000)
    normalized_key: str | None = Field(default=None, max_length=120)


class JobOffer(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    canonical_url: HttpUrl
    platform: str = Field(min_length=1, max_length=120)
    employer: str = Field(min_length=1, max_length=240)
    title: str = Field(min_length=1, max_length=240)
    location: str = Field(min_length=1, max_length=240)
    employment_type: str = Field(min_length=1, max_length=120)
    published_on: date | None = None
    publication_date_reliability: PublicationDateReliability
    raw_description: str = Field(min_length=1)
    requirements: list[JobRequirement] = Field(default_factory=list)
    deduplication_fingerprint: str
    captured_at: AwareDatetime = Field(default_factory=utc_now)

    @field_validator("deduplication_fingerprint")
    @classmethod
    def valid_sha256(cls, value: str) -> str:
        if not SHA256_PATTERN.fullmatch(value):
            raise ValueError("deduplication_fingerprint must be a lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def validate_publication_date(self) -> "JobOffer":
        if self.published_on is None and self.publication_date_reliability not in (
            PublicationDateReliability.UNKNOWN,
            PublicationDateReliability.RELATIVE,
        ):
            raise ValueError("A verified/platform publication date requires an absolute date")
        if self.published_on is not None and self.publication_date_reliability in (
            PublicationDateReliability.UNKNOWN,
            PublicationDateReliability.RELATIVE,
        ):
            raise ValueError("An absolute publication date needs VERIFIED or PLATFORM_CLAIM reliability")
        return self


class AssessmentItem(DomainModel):
    statement: str = Field(min_length=1, max_length=1000)
    source_refs: list[str] = Field(min_length=1)


class MatchAssessment(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    job_offer_id: UUID
    score: int = Field(ge=0, le=100)
    decision: MatchDecision
    matches: list[AssessmentItem] = Field(default_factory=list)
    gaps: list[AssessmentItem] = Field(default_factory=list)
    blockers: list[AssessmentItem] = Field(default_factory=list)
    unknowns: list[AssessmentItem] = Field(default_factory=list)
    recommended_projects: list[str] = Field(default_factory=list)
    prompt_version: str = Field(min_length=1, max_length=120)
    model_used: str = Field(min_length=1, max_length=120)
    assessed_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_decision_coherence(self) -> "MatchAssessment":
        if self.score == 0 and self.matches:
            raise ValueError("A zero score cannot coexist with factual matches")
        if self.score == 100 and (self.gaps or self.blockers or self.unknowns):
            raise ValueError("A perfect score cannot coexist with gaps, blockers or unknowns")
        if self.decision == MatchDecision.APPLY and self.blockers:
            raise ValueError("APPLY cannot contain a confirmed blocker")
        if self.decision == MatchDecision.REJECT and self.score >= 80 and not self.blockers:
            raise ValueError("High-score REJECT requires a confirmed blocker")
        if self.decision == MatchDecision.REVIEW and not (
            self.gaps or self.blockers or self.unknowns
        ):
            raise ValueError("REVIEW requires a documented gap, blocker or unknown")
        return self


class DocumentArtifact(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    application_id: UUID
    document_type: DocumentType
    path: Path
    sha256: str
    version: str = Field(min_length=1, max_length=80)
    generator_source: str = Field(min_length=1, max_length=240)
    visual_validation: VisualValidationStatus = VisualValidationStatus.PENDING
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: Path) -> Path:
        return _assert_safe_path(value)

    @field_validator("sha256")
    @classmethod
    def valid_sha256(cls, value: str) -> str:
        if not SHA256_PATTERN.fullmatch(value):
            raise ValueError("sha256 must be a lowercase SHA-256")
        return value


class FormAnswer(DomainModel):
    value: JsonValue | None
    source_fact_keys: list[str] = Field(default_factory=list)
    verified: bool = False
    sensitive: bool = False


class SubmissionReceipt(DomainModel):
    kind: ReceiptKind
    reference: str = Field(min_length=1, max_length=1000)
    captured_at: AwareDatetime = Field(default_factory=utc_now)
    evidence_path: Path | None = None

    @field_validator("evidence_path")
    @classmethod
    def safe_evidence_path(cls, value: Path | None) -> Path | None:
        return None if value is None else _assert_safe_path(value)


class Application(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    job_offer_id: UUID
    # Frozen: only the state machine may produce a new state, through a validated copy.
    state: ApplicationState = Field(default=ApplicationState.DISCOVERED, frozen=True)
    state_version: int = Field(default=0, ge=0, frozen=True)
    last_transition_id: UUID | None = Field(default=None, frozen=True)
    assessment_id: UUID | None = None
    local_folder: Path
    document_ids: list[UUID] = Field(default_factory=list)
    form_answers: dict[str, FormAnswer] = Field(default_factory=dict)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    submitted_at: AwareDatetime | None = None
    receipt: SubmissionReceipt | None = None
    next_action: str | None = Field(default=None, max_length=1000)
    awaiting_reason: str | None = Field(default=None, max_length=1000)
    resume_state: ApplicationState | None = None
    failed_from: ApplicationState | None = None
    failure_kind: FailureKind | None = None
    failure_reason: str | None = Field(default=None, max_length=2000)

    @field_validator("local_folder")
    @classmethod
    def safe_local_folder(cls, value: Path) -> Path:
        return _assert_safe_path(value)

    @field_validator("form_answers")
    @classmethod
    def reject_secret_form_fields(
        cls, value: dict[str, FormAnswer]
    ) -> dict[str, FormAnswer]:
        forbidden = _contains_forbidden_key(value)
        if forbidden:
            raise ValueError(f"Form answers must not store secret field {forbidden!r}")
        return value

    @model_validator(mode="after")
    def validate_submission_evidence(self) -> "Application":
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if self.state in (ApplicationState.SUBMITTED, ApplicationState.CONFIRMED):
            if self.submitted_at is None:
                raise ValueError("Submitted or confirmed applications require submitted_at")
        if self.state == ApplicationState.CONFIRMED and self.receipt is None:
            raise ValueError("CONFIRMED requires visible page, portal or email evidence")
        if self.receipt is not None and self.state not in (
            ApplicationState.SUBMITTED,
            ApplicationState.CONFIRMED,
        ):
            raise ValueError("A submission receipt is invalid before submission")
        return self

    @model_validator(mode="after")
    def validate_pause_and_failure_context(self) -> "Application":
        not_resumable = (
            ApplicationState.AWAITING_USER,
            ApplicationState.FAILED,
            ApplicationState.REJECTED,
            ApplicationState.CONFIRMED,
            ApplicationState.WITHDRAWN,
        )
        if self.state == ApplicationState.AWAITING_USER:
            if not self.awaiting_reason or self.resume_state is None:
                raise ValueError("AWAITING_USER requires awaiting_reason and an explicit resume_state")
            if self.resume_state in not_resumable:
                raise ValueError(f"resume_state {self.resume_state} is not a resumable work state")
        elif self.awaiting_reason is not None or self.resume_state is not None:
            raise ValueError("awaiting_reason/resume_state are only valid in AWAITING_USER")
        failure_fields = (self.failed_from, self.failure_kind, self.failure_reason)
        if self.state == ApplicationState.FAILED:
            if any(value is None for value in failure_fields):
                raise ValueError("FAILED requires failed_from, failure_kind and failure_reason")
            if self.failed_from in not_resumable:
                raise ValueError(f"failed_from {self.failed_from} is not a work state")
            if (
                self.failed_from in (ApplicationState.READY_TO_SUBMIT, ApplicationState.SUBMITTED)
                and self.failure_kind == FailureKind.RETRYABLE_IDEMPOTENT
            ):
                raise ValueError("A submission failure is never retryable automatically")
        elif any(value is not None for value in failure_fields):
            raise ValueError("failure fields are only valid in FAILED")
        return self


class ActionEvidence(DomainModel):
    kind: str = Field(min_length=1, max_length=120)
    reference: str = Field(min_length=1, max_length=1000)


class ActionLog(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    action: str = Field(min_length=1, max_length=240)
    target: str = Field(min_length=1, max_length=1000)
    redacted_input: dict[str, JsonValue] = Field(default_factory=dict)
    result: ActionResult
    evidence: list[ActionEvidence] = Field(default_factory=list)
    occurred_at: AwareDatetime = Field(default_factory=utc_now)
    error: str | None = Field(default=None, max_length=4000)
    idempotent: bool
    safe_to_retry: bool

    @field_validator("redacted_input")
    @classmethod
    def enforce_redaction(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        forbidden = _contains_forbidden_key(value)
        if forbidden:
            raise ValueError(f"ActionLog contains forbidden secret field {forbidden!r}")
        return value

    @model_validator(mode="after")
    def validate_result_and_retry(self) -> "ActionLog":
        if self.result == ActionResult.FAILED and not self.error:
            raise ValueError("FAILED actions require an error")
        if self.result == ActionResult.SUCCESS and self.error:
            raise ValueError("SUCCESS actions cannot contain an error")
        if self.safe_to_retry and not self.idempotent:
            raise ValueError("A non-idempotent action is never automatically safe to retry")
        return self
