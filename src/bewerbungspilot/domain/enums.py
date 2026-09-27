"""Stable business vocabulary shared by persistence, API and workflows."""

from enum import StrEnum


class FactCategory(StrEnum):
    IDENTITY = "IDENTITY"
    CONTACT = "CONTACT"
    EDUCATION = "EDUCATION"
    EXPERIENCE = "EXPERIENCE"
    SKILL = "SKILL"
    LANGUAGE = "LANGUAGE"
    AVAILABILITY = "AVAILABILITY"
    COMPENSATION = "COMPENSATION"
    ADMINISTRATIVE = "ADMINISTRATIVE"
    PREFERENCE = "PREFERENCE"
    OTHER = "OTHER"
    # Phase 3.1 — categories required by the memory guide.
    PROJECT = "PROJECT"
    LEGAL_SENSITIVE = "LEGAL_SENSITIVE"
    SUPPORTING_DOCUMENT = "SUPPORTING_DOCUMENT"
    ACTION_RULE = "ACTION_RULE"


class FactStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    UNKNOWN = "UNKNOWN"
    EXPIRED = "EXPIRED"
    SENSITIVE = "SENSITIVE"


class DisclosurePolicy(StrEnum):
    # Phase 3.2 policies (preferred).
    PUBLIC_PROFILE = "PUBLIC_PROFILE"
    APPLICATION_STANDARD = "APPLICATION_STANDARD"
    FORM_REQUIRED_ONLY = "FORM_REQUIRED_ONLY"
    ASK_BEFORE_USE = "ASK_BEFORE_USE"
    NEVER_EXPORT = "NEVER_EXPORT"
    # Phase 2.4 policies, kept readable; see LEGACY_POLICY_MAP.
    PUBLIC = "PUBLIC"
    APPLICATION_ONLY = "APPLICATION_ONLY"
    EXPLICIT_REQUEST_ONLY = "EXPLICIT_REQUEST_ONLY"
    NEVER_AUTOFILL = "NEVER_AUTOFILL"


LEGACY_POLICY_MAP = {
    DisclosurePolicy.PUBLIC: DisclosurePolicy.PUBLIC_PROFILE,
    DisclosurePolicy.APPLICATION_ONLY: DisclosurePolicy.APPLICATION_STANDARD,
    DisclosurePolicy.EXPLICIT_REQUEST_ONLY: DisclosurePolicy.FORM_REQUIRED_ONLY,
    DisclosurePolicy.NEVER_AUTOFILL: DisclosurePolicy.ASK_BEFORE_USE,
}


def normalize_policy(policy: DisclosurePolicy) -> DisclosurePolicy:
    """Map a phase 2.4 policy to its phase 3.2 equivalent; new policies are unchanged."""

    return LEGACY_POLICY_MAP.get(policy, policy)


RESTRICTED_POLICIES = frozenset(
    {
        DisclosurePolicy.FORM_REQUIRED_ONLY,
        DisclosurePolicy.ASK_BEFORE_USE,
        DisclosurePolicy.NEVER_EXPORT,
    }
)


class SourceRank(StrEnum):
    """Source kinds, from most to least authoritative (guide 3.5)."""

    USER_CORRECTION = "USER_CORRECTION"
    OFFICIAL_DOCUMENT = "OFFICIAL_DOCUMENT"
    REFERENCE_PROFILE = "REFERENCE_PROFILE"
    VALIDATED_DOCUMENT = "VALIDATED_DOCUMENT"
    OLD_APPLICATION = "OLD_APPLICATION"
    MODEL_INFERENCE = "MODEL_INFERENCE"


SOURCE_PRIORITY = {rank: index for index, rank in enumerate(SourceRank)}


class PublicationDateReliability(StrEnum):
    VERIFIED = "VERIFIED"
    PLATFORM_CLAIM = "PLATFORM_CLAIM"
    RELATIVE = "RELATIVE"
    UNKNOWN = "UNKNOWN"


class RequirementLevel(StrEnum):
    REQUIRED = "REQUIRED"
    PREFERRED = "PREFERRED"
    INFORMATIONAL = "INFORMATIONAL"


class MatchDecision(StrEnum):
    APPLY = "APPLY"
    REVIEW = "REVIEW"
    REJECT = "REJECT"


class ApplicationState(StrEnum):
    DISCOVERED = "DISCOVERED"
    EVALUATED = "EVALUATED"
    REJECTED = "REJECTED"
    SELECTED = "SELECTED"
    DOCUMENTS_PREPARED = "DOCUMENTS_PREPARED"
    FORM_IN_PROGRESS = "FORM_IN_PROGRESS"
    AWAITING_USER = "AWAITING_USER"
    READY_TO_SUBMIT = "READY_TO_SUBMIT"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    WITHDRAWN = "WITHDRAWN"


class DocumentType(StrEnum):
    CV = "CV"
    COVER_LETTER = "COVER_LETTER"
    TRANSCRIPT = "TRANSCRIPT"
    CERTIFICATE = "CERTIFICATE"
    PORTFOLIO = "PORTFOLIO"
    OTHER = "OTHER"


class VisualValidationStatus(StrEnum):
    PENDING = "PENDING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ReceiptKind(StrEnum):
    PAGE = "PAGE"
    EMAIL = "EMAIL"
    PORTAL = "PORTAL"


class ActionResult(StrEnum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    AWAITING_USER = "AWAITING_USER"
    SKIPPED = "SKIPPED"


class TransitionActor(StrEnum):
    SYSTEM = "SYSTEM"
    USER = "USER"


class FailureKind(StrEnum):
    RETRYABLE_IDEMPOTENT = "RETRYABLE_IDEMPOTENT"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    PERMANENT = "PERMANENT"
