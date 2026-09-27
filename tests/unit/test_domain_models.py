from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from bewerbungspilot.domain import (
    ActionLog,
    ActionResult,
    Application,
    ApplicationState,
    AssessmentItem,
    CandidateFact,
    DisclosurePolicy,
    DocumentArtifact,
    DocumentType,
    FactCategory,
    FactStatus,
    FormAnswer,
    JobOffer,
    MatchAssessment,
    MatchDecision,
    PublicationDateReliability,
    ReceiptKind,
    SubmissionReceipt,
    VisualValidationStatus,
)

NOW = datetime(2026, 9, 26, 18, 0, tzinfo=timezone.utc)
SHA = "a" * 64


def item(statement: str = "Python is confirmed") -> AssessmentItem:
    return AssessmentItem(statement=statement, source_refs=["offer:req:python", "fact:skill.python"])


def test_confirmed_fact_requires_source_and_validation_date() -> None:
    with pytest.raises(ValidationError):
        CandidateFact(
            key="skill.python",
            value="intermediate",
            category=FactCategory.SKILL,
            status=FactStatus.CONFIRMED,
            disclosure_policy=DisclosurePolicy.APPLICATION_ONLY,
        )


def test_unknown_fact_cannot_assert_a_value() -> None:
    with pytest.raises(ValidationError):
        CandidateFact(
            key="skill.aws.years",
            value=2,
            category=FactCategory.SKILL,
            status=FactStatus.UNKNOWN,
            disclosure_policy=DisclosurePolicy.NEVER_AUTOFILL,
        )


def test_sensitive_fact_cannot_be_public_or_automatically_disclosed() -> None:
    with pytest.raises(ValidationError):
        CandidateFact(
            key="administrative.document_expiry",
            value=date(2026, 11, 14),
            category=FactCategory.ADMINISTRATIVE,
            status=FactStatus.SENSITIVE,
            source="private document",
            validated_at=NOW,
            disclosure_policy=DisclosurePolicy.PUBLIC,
        )


def test_sensitive_fact_with_explicit_request_policy_is_valid() -> None:
    fact = CandidateFact(
        key="administrative.document_expiry",
        value=date(2026, 11, 14),
        category=FactCategory.ADMINISTRATIVE,
        status=FactStatus.SENSITIVE,
        source="private document",
        validated_at=NOW,
        disclosure_policy=DisclosurePolicy.EXPLICIT_REQUEST_ONLY,
    )
    assert fact.status == FactStatus.SENSITIVE


def test_job_offer_date_reliability_requires_an_absolute_date() -> None:
    with pytest.raises(ValidationError):
        JobOffer(
            canonical_url="https://example.test/job/1",
            platform="Official",
            employer="Example GmbH",
            title="Werkstudent Software",
            location="Gießen",
            employment_type="Werkstudent",
            publication_date_reliability=PublicationDateReliability.VERIFIED,
            raw_description="Fictional test offer",
            deduplication_fingerprint=SHA,
        )


def test_job_offer_accepts_unknown_date_without_inventing_one() -> None:
    offer = JobOffer(
        canonical_url="https://example.test/job/1",
        platform="Official",
        employer="Example GmbH",
        title="Werkstudent Software",
        location="Gießen",
        employment_type="Werkstudent",
        publication_date_reliability=PublicationDateReliability.UNKNOWN,
        raw_description="Fictional test offer",
        deduplication_fingerprint=SHA,
    )
    assert offer.published_on is None


def test_job_offer_rejects_invalid_fingerprint() -> None:
    with pytest.raises(ValidationError):
        JobOffer(
            canonical_url="https://example.test/job/1",
            platform="Official",
            employer="Example GmbH",
            title="Werkstudent Software",
            location="Gießen",
            employment_type="Werkstudent",
            publication_date_reliability=PublicationDateReliability.UNKNOWN,
            raw_description="Fictional test offer",
            deduplication_fingerprint="not-a-sha",
        )


def test_zero_match_score_cannot_have_factual_matches() -> None:
    with pytest.raises(ValidationError):
        MatchAssessment(
            job_offer_id=uuid4(),
            score=0,
            decision=MatchDecision.REVIEW,
            matches=[item()],
            unknowns=[item("Weekly hours unknown")],
            prompt_version="v1",
            model_used="test:1b",
        )


def test_apply_cannot_hide_a_blocker() -> None:
    with pytest.raises(ValidationError):
        MatchAssessment(
            job_offer_id=uuid4(),
            score=80,
            decision=MatchDecision.APPLY,
            blockers=[item("Mandatory degree missing")],
            prompt_version="v1",
            model_used="test:1b",
        )


def test_review_requires_a_documented_reason() -> None:
    with pytest.raises(ValidationError):
        MatchAssessment(
            job_offer_id=uuid4(),
            score=70,
            decision=MatchDecision.REVIEW,
            matches=[item()],
            prompt_version="v1",
            model_used="test:1b",
        )


def test_valid_review_preserves_traceable_sources() -> None:
    assessment = MatchAssessment(
        job_offer_id=uuid4(),
        score=70,
        decision=MatchDecision.REVIEW,
        matches=[item()],
        unknowns=[item("Start date unknown")],
        recommended_projects=["Data / Machine Learning"],
        prompt_version="v1",
        model_used="test:1b",
    )
    assert assessment.matches[0].source_refs == ["offer:req:python", "fact:skill.python"]


def test_document_requires_sha256_and_safe_path() -> None:
    with pytest.raises(ValidationError):
        DocumentArtifact(
            application_id=uuid4(),
            document_type=DocumentType.CV,
            path=Path("..") / "other" / "cv.pdf",
            sha256=SHA,
            version="1",
            generator_source="template:test",
        )


def test_valid_document_records_visual_review_state() -> None:
    document = DocumentArtifact(
        application_id=uuid4(),
        document_type=DocumentType.COVER_LETTER,
        path=Path("data/generated/application/letter.pdf"),
        sha256=SHA,
        version="1",
        generator_source="template:test",
        visual_validation=VisualValidationStatus.PASSED,
    )
    assert document.visual_validation == VisualValidationStatus.PASSED


def test_confirmed_application_requires_receipt() -> None:
    with pytest.raises(ValidationError):
        Application(
            job_offer_id=uuid4(),
            state=ApplicationState.CONFIRMED,
            local_folder=Path("data/generated/app-1"),
            submitted_at=NOW,
        )


def test_confirmed_application_with_receipt_is_valid() -> None:
    application = Application(
        job_offer_id=uuid4(),
        state=ApplicationState.CONFIRMED,
        local_folder=Path("data/generated/app-1"),
        created_at=NOW,
        updated_at=NOW + timedelta(minutes=2),
        submitted_at=NOW + timedelta(minutes=1),
        receipt=SubmissionReceipt(
            kind=ReceiptKind.EMAIL,
            reference="message-id:test-confirmation",
            captured_at=NOW + timedelta(minutes=2),
        ),
    )
    assert application.receipt is not None


def test_application_never_stores_login_secrets_as_form_answers() -> None:
    with pytest.raises(ValidationError):
        Application(
            job_offer_id=uuid4(),
            local_folder=Path("data/generated/app-1"),
            form_answers={"password": FormAnswer(value="secret")},
        )


def test_action_log_rejects_secret_keys_even_when_nested() -> None:
    with pytest.raises(ValidationError):
        ActionLog(
            run_id=uuid4(),
            action="fill_form",
            target="fictional portal",
            redacted_input={"account": {"access_token": "secret"}},
            result=ActionResult.SUCCESS,
            idempotent=True,
            safe_to_retry=False,
        )


def test_non_idempotent_action_is_never_automatically_safe_to_retry() -> None:
    with pytest.raises(ValidationError):
        ActionLog(
            run_id=uuid4(),
            action="submit_application",
            target="fictional portal",
            result=ActionResult.FAILED,
            error="Network response unknown",
            idempotent=False,
            safe_to_retry=True,
        )


def test_failed_action_requires_an_error() -> None:
    with pytest.raises(ValidationError):
        ActionLog(
            run_id=uuid4(),
            action="read_offer",
            target="fictional page",
            result=ActionResult.FAILED,
            idempotent=True,
            safe_to_retry=True,
        )


def test_successful_idempotent_action_can_be_retried() -> None:
    log = ActionLog(
        run_id=uuid4(),
        action="read_offer",
        target="fictional page",
        redacted_input={"job_id": "123"},
        result=ActionResult.SUCCESS,
        idempotent=True,
        safe_to_retry=True,
    )
    assert log.safe_to_retry is True
