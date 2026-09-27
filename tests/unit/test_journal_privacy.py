"""Phase 2.6 — separate journals, run_id traceability, rotation and no secret on disk."""

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from bewerbungspilot.core.journal import (
    BusinessJournal,
    RunContext,
    TechnicalJournal,
    read_journal,
)
from bewerbungspilot.core.secrets import REDACTED, contains_forbidden_key, is_secret_key, redact
from bewerbungspilot.domain import (
    ApplicationState as S,
    DocumentArtifact,
    DocumentType,
    ReceiptKind,
    SubmissionReceipt,
    TransitionActor,
    TransitionRecord,
    VisualValidationStatus,
)
from bewerbungspilot.domain.models import _contains_forbidden_key
from bewerbungspilot.llm.failure_store import FileFailureStore

NOW = datetime(2026, 9, 26, 20, 0, tzinfo=timezone.utc)

# Known secret values: none of them may ever appear in a written file.
KNOWN_SECRETS = [
    "hunter2-Pa55word",
    "483920",
    "sess-9f8e7d6c5b4a3210",
    "ya29.A0ARrdaM-very-secret-token",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "ghp_" + ("x" * 24),
    "DE89 3704 0044 0532 0130 00",
]

SECRET_PAYLOAD = {
    "portal": "demo-ats.example",
    "password": KNOWN_SECRETS[0],
    "nested": {"otp": KNOWN_SECRETS[1], "headers": {"Cookie": f"session={KNOWN_SECRETS[2]}"}},
    "list": [{"access_token": KNOWN_SECRETS[3]}, f"Authorization: Bearer {KNOWN_SECRETS[4]}"],
    "free_text": f"Login ok, password={KNOWN_SECRETS[0]} code: {KNOWN_SECRETS[1]} pat {KNOWN_SECRETS[5]}",
    "bank": f"IBAN {KNOWN_SECRETS[6]}",
    "residence_permit_number": "X12345678",
    "prompt_tokens": 42,
}


def assert_no_known_secret(directory: Path) -> None:
    for file in directory.rglob("*"):
        if file.is_file():
            content = file.read_text(encoding="utf-8")
            for secret in KNOWN_SECRETS + ["X12345678"]:
                assert secret not in content, f"{secret!r} leaked into {file}"


def test_redact_is_recursive_and_keeps_harmless_fields() -> None:
    safe = redact(SECRET_PAYLOAD)
    assert safe["password"] == REDACTED
    assert safe["nested"]["otp"] == REDACTED
    assert safe["nested"]["headers"]["Cookie"] == REDACTED
    assert safe["list"][0]["access_token"] == REDACTED
    assert safe["portal"] == "demo-ats.example"
    assert safe["prompt_tokens"] == 42
    dumped = json.dumps(safe)
    for secret in KNOWN_SECRETS:
        assert secret not in dumped
    assert SECRET_PAYLOAD["password"] == KNOWN_SECRETS[0]  # the input is not mutated


@pytest.mark.parametrize("key", ["password", "Session-Cookie", "refresh_token", "api.key", "user_pin"])
def test_secret_keys_are_detected(key: str) -> None:
    assert is_secret_key(key)


@pytest.mark.parametrize("key", ["prompt_tokens", "footprint", "pinned", "employer", "eval_count"])
def test_harmless_keys_are_not_redacted(key: str) -> None:
    assert not is_secret_key(key)


def test_models_reuse_the_shared_secret_detector() -> None:
    assert _contains_forbidden_key is contains_forbidden_key


def test_technical_and_business_journals_are_separate_and_scrubbed(tmp_path: Path) -> None:
    run = RunContext(purpose="test run")
    with TechnicalJournal(tmp_path) as tech, BusinessJournal(tmp_path) as business:
        tech.record(run, "llm.ollama", "request_failed", duration_seconds=1.5, error="timeout", payload=SECRET_PAYLOAD)
        business.record_offer_decision(run, uuid4(), "REJECT", "Vollzeit only; " + SECRET_PAYLOAD["free_text"])
    tech_lines = read_journal(tmp_path / "technical" / "technical.jsonl")
    business_lines = read_journal(tmp_path / "business" / "business.jsonl")
    assert [line["event"] for line in tech_lines] == ["request_failed"]
    assert [line["event"] for line in business_lines] == ["offer_decision"]
    assert tech_lines[0]["level"] == "ERROR" and tech_lines[0]["module"] == "llm.ollama"
    assert_no_known_secret(tmp_path)


def test_every_line_and_transition_is_attached_to_the_run(tmp_path: Path) -> None:
    run = RunContext(purpose="application acme")
    application_id = uuid4()
    record = TransitionRecord(
        execution_id=uuid4(),
        application_id=application_id,
        from_state=S.READY_TO_SUBMIT,
        to_state=S.SUBMITTED,
        version_before=5,
        version_after=6,
        actor=TransitionActor.SYSTEM,
        reason="authorised submission",
        occurred_at=NOW,
        run_id=run.run_id,
    )
    document = DocumentArtifact(
        application_id=application_id,
        document_type=DocumentType.CV,
        path=Path("data/generated/acme/cv.pdf"),
        sha256="c" * 64,
        version="v2",
        generator_source="template:cv:v1",
        visual_validation=VisualValidationStatus.PASSED,
    )
    receipt = SubmissionReceipt(kind=ReceiptKind.EMAIL, reference="Eingangsbestätigung #42", captured_at=NOW)
    with BusinessJournal(tmp_path) as journal:
        journal.record_transition(run, record)
        journal.record_document_sent(run, application_id, document, "portal-upload")
        journal.record_receipt(run, application_id, receipt)
    lines = read_journal(tmp_path / "business" / "business.jsonl")
    assert {line["run_id"] for line in lines} == {str(run.run_id)}
    assert lines[0]["from_state"] == "READY_TO_SUBMIT" and lines[0]["to_state"] == "SUBMITTED"
    assert lines[1]["sha256"] == "c" * 64
    assert lines[2]["reference"] == "Eingangsbestätigung #42"


def test_transition_from_another_run_is_refused(tmp_path: Path) -> None:
    record = TransitionRecord(
        execution_id=uuid4(),
        application_id=uuid4(),
        from_state=S.DISCOVERED,
        to_state=S.EVALUATED,
        version_before=0,
        version_after=1,
        actor=TransitionActor.SYSTEM,
        reason="assessed",
        occurred_at=NOW,
        run_id=uuid4(),
    )
    with BusinessJournal(tmp_path) as journal, pytest.raises(ValueError):
        journal.record_transition(RunContext(purpose="other"), record)


def test_journal_rotates_locally(tmp_path: Path) -> None:
    run = RunContext(purpose="rotation")
    with TechnicalJournal(tmp_path, max_bytes=600, backup_count=2) as journal:
        for index in range(40):
            journal.record(run, "test", "tick", index=index, filler="x" * 50)
    files = sorted(p.name for p in (tmp_path / "technical").iterdir())
    assert files == ["technical.jsonl", "technical.jsonl.1", "technical.jsonl.2"]


def test_failure_store_keeps_raw_response_but_scrubs_secrets(tmp_path: Path) -> None:
    run_id = uuid4()
    raw = {"model": "test:1b", "response": "{not json", "done": True, "echo": SECRET_PAYLOAD}
    path = FileFailureStore(tmp_path).record("structured_initial", {"raw": raw}, run_id)
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["run_id"] == str(run_id)
    assert stored["payload"]["raw"]["response"] == "{not json"  # raw model output is kept
    assert_no_known_secret(tmp_path)
