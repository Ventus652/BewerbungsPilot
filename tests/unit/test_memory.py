"""Phase 3 — categories/policies, import, store, priority, minimal context, rules.

All data comes from the fictional profile in ``tests/fixtures/memory_root``.
"""

import json
import shutil
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from bewerbungspilot.domain import (
    LEGACY_POLICY_MAP,
    Application,
    ApplicationState,
    CandidateFact,
    DisclosurePolicy as P,
    DocumentType,
    FactCategory as C,
    FactStatus,
    ReceiptKind,
    SourceRank,
    SubmissionReceipt,
    normalize_policy,
)
from bewerbungspilot.memory.context import TaskType, build_context
from bewerbungspilot.memory.importer import import_sources
from bewerbungspilot.memory.priority import merge
from bewerbungspilot.memory.records import ConflictStatus, MemorySnapshot
from bewerbungspilot.memory.rules import ProfileRules, RuleOutcome, blocking_unknowns
from bewerbungspilot.memory.store import MemoryStore, SecretInMemoryError

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "memory_root"
TODAY = date(2026, 9, 26)
NOW = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
ADDRESS = "Musterweg 1, 12345 Musterstadt, Allemagne"


@pytest.fixture(scope="module")
def imported():
    return import_sources(FIXTURE_ROOT)


@pytest.fixture
def memory(imported, tmp_path) -> MemorySnapshot:
    store = MemoryStore(tmp_path / "private")
    snapshot, _ = store.ingest(MemorySnapshot(), imported.facts, imported.sources, run_id=uuid4(), kind="import")
    store.save(snapshot)
    return store.load()


def fact(key: str, value, *, rank=SourceRank.REFERENCE_PROFILE, **extra) -> CandidateFact:
    data = dict(
        key=key,
        value=value,
        category=C.AVAILABILITY,
        status=FactStatus.CONFIRMED,
        source=f"test:{rank}",
        source_rank=rank,
        validated_at=NOW,
        disclosure_policy=P.APPLICATION_STANDARD,
    )
    data.update(extra)
    return CandidateFact(**data)


# =============================================================== 3.1–3.2 categories & policies
def test_guide_categories_exist() -> None:
    for name in ("IDENTITY", "CONTACT", "EDUCATION", "EXPERIENCE", "PROJECT", "SKILL", "LANGUAGE",
                 "AVAILABILITY", "PREFERENCE", "COMPENSATION", "LEGAL_SENSITIVE", "SUPPORTING_DOCUMENT", "ACTION_RULE"):
        assert C(name)


def test_legacy_policies_map_explicitly_to_new_ones() -> None:
    assert LEGACY_POLICY_MAP == {
        P.PUBLIC: P.PUBLIC_PROFILE,
        P.APPLICATION_ONLY: P.APPLICATION_STANDARD,
        P.EXPLICIT_REQUEST_ONLY: P.FORM_REQUIRED_ONLY,
        P.NEVER_AUTOFILL: P.ASK_BEFORE_USE,
    }
    assert normalize_policy(P.NEVER_EXPORT) == P.NEVER_EXPORT


def test_fact_can_be_confirmed_and_sensitive() -> None:
    permit = fact("legal.permit_until", "2027-03-31", category=C.LEGAL_SENSITIVE, sensitive=True,
                  disclosure_policy=P.FORM_REQUIRED_ONLY)
    assert permit.status == FactStatus.CONFIRMED and permit.is_sensitive


@pytest.mark.parametrize("policy", [P.PUBLIC_PROFILE, P.APPLICATION_STANDARD, P.PUBLIC, P.APPLICATION_ONLY])
def test_sensitive_fact_rejects_open_policies(policy: P) -> None:
    with pytest.raises(ValidationError):
        fact("legal.permit_until", "2027-03-31", category=C.LEGAL_SENSITIVE, sensitive=True, disclosure_policy=policy)


def test_legal_category_must_be_sensitive() -> None:
    with pytest.raises(ValidationError):
        fact("legal.permit_until", "2027-03-31", category=C.LEGAL_SENSITIVE, disclosure_policy=P.FORM_REQUIRED_ONLY)


def test_model_inference_never_confirms() -> None:
    with pytest.raises(ValidationError):
        fact("skill.aws", "expert", rank=SourceRank.MODEL_INFERENCE, category=C.SKILL)


def test_expired_fact_is_kept_but_never_current() -> None:
    old = fact("availability.start_date", "2025-10-01", status=FactStatus.EXPIRED)
    assert not old.is_current(TODAY)
    dated = fact("availability.start_date", "2026-11-01", valid_until=date(2026, 1, 1))
    assert not dated.is_current(TODAY)


# =============================================================== 3.3 import
def test_import_reads_reference_files_with_line_sources(imported) -> None:
    by_key = {f.key: f for f in imported.facts if f.source_rank == SourceRank.REFERENCE_PROFILE and f.source_ref.startswith("profile")}
    assert by_key["availability.hours_per_week"].value == 20
    assert by_key["availability.start_date"].value == "2026-11-01"
    assert by_key["compensation.hourly_gross_eur"].value == 15.0
    assert by_key["education.bachelor_expected_end"].value == "2027-09-30"
    assert by_key["legal.residence_permit_valid_until"].is_sensitive
    assert by_key["legal.residence_permit_renewal_confirmed"].value is False
    assert by_key["skill.docker"].value == {"name": "Docker", "level": "basics"}
    assert by_key["preference.onsite_locations"].value == ["Musterstadt", "Beispielburg", "Testdorf", "Frankfurt"]
    assert "Kubernetes" in [str(v) for v in by_key["action_rule.forbidden_claims"].value][2]
    for f in imported.facts:
        assert f.source and f.source_ref and f.validated_at  # traceable


def test_import_never_modifies_sources(tmp_path) -> None:
    root = tmp_path / "copy"
    shutil.copytree(FIXTURE_ROOT, root)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    import_sources(root)
    assert before == {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_old_dossier_facts_are_never_directly_usable(imported) -> None:
    legal_name = next(f for f in imported.facts if f.key == "identity.legal_name")
    assert legal_name.source_rank == SourceRank.OLD_APPLICATION and legal_name.needs_review
    (entry,) = imported.applications
    assert entry["company"] == "Demo GmbH" and entry["sent"] is True


# =============================================================== 3.4–3.5 store & priority
def test_same_authority_conflict_stays_open_and_blocks_the_key(memory) -> None:
    gpa = [c for c in memory.conflicts if c.key == "education.gpa"]
    assert len(gpa) == 1 and gpa[0].status == ConflictStatus.OPEN
    assert {v.value for v in gpa[0].values} == {2.5, 2.7}
    assert memory.fact("education.gpa").needs_review


def test_priority_resolves_objectively_and_keeps_the_conflict_visible(memory) -> None:
    end = next(c for c in memory.conflicts if c.key == "education.bachelor_expected_end")
    assert end.status == ConflictStatus.RESOLVED_BY_PRIORITY
    assert end.retained_value == "2027-09-30"
    assert memory.fact("education.bachelor_expected_end").source_rank == SourceRank.REFERENCE_PROFILE


def test_date_conflict_between_equal_sources_is_not_chosen_arbitrarily() -> None:
    a = fact("availability.start_date", "2026-11-01")
    b = fact("availability.start_date", "2026-12-01")
    outcome = merge(a, b)
    assert outcome.conflict.status == ConflictStatus.OPEN
    assert outcome.retained.needs_review and not outcome.retained.is_current(TODAY)


def test_user_correction_outranks_everything_and_inference_nothing() -> None:
    reference = fact("availability.start_date", "2026-11-01")
    user = fact("availability.start_date", "2026-12-01", rank=SourceRank.USER_CORRECTION)
    assert merge(reference, user).retained.value == "2026-12-01"
    guess = fact("availability.start_date", None, rank=SourceRank.MODEL_INFERENCE, status=FactStatus.UNKNOWN)
    assert merge(reference, guess).retained is reference


def test_user_resolution_unblocks_the_key(memory, tmp_path) -> None:
    store = MemoryStore(tmp_path / "private")
    conflict = next(c for c in memory.conflicts if c.key == "education.gpa")
    resolved, migration = store.resolve_conflict(memory, conflict.id, 2.5, run_id=uuid4(), decided_on=TODAY)
    gpa = resolved.fact("education.gpa")
    assert gpa.value == 2.5 and gpa.source_rank == SourceRank.USER_CORRECTION and not gpa.needs_review
    assert "education.gpa" not in resolved.open_conflict_keys()
    assert migration.kind == "user_correction"


def test_export_and_restore_round_trip(memory, tmp_path) -> None:
    store = MemoryStore(tmp_path / "private")
    exported = store.export(tmp_path / "export.json")
    data = json.loads(exported.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1 and len(data["facts"]) == len(memory.facts)
    other = MemoryStore(tmp_path / "restored")
    restored = other.restore(exported)
    assert restored.model_dump(mode="json")["facts"] == memory.model_dump(mode="json")["facts"]
    assert {p.name for p in (tmp_path / "restored").iterdir()} >= {"profile.json", "sources.json", "conflicts.json", "policies.json", "migrations"}


def test_every_exported_fact_keeps_source_and_validation_date(memory, tmp_path) -> None:
    exported = MemoryStore(tmp_path / "private").export(tmp_path / "export.json")
    for item in json.loads(exported.read_text(encoding="utf-8"))["facts"]:
        assert item["source"] and item["validated_at"], item["key"]


@pytest.mark.parametrize(
    "bad",
    [
        {"key": "portal.password", "value": "x"},
        {"key": "portal.login", "value": {"password": "hunter2"}},
        {"key": "portal.note", "value": "access_token=abc123456789"},
    ],
)
def test_store_refuses_secrets(bad, tmp_path) -> None:
    with pytest.raises(SecretInMemoryError):
        MemoryStore(tmp_path).ingest(MemorySnapshot(), [fact(bad["key"], bad["value"], category=C.OTHER)], [], run_id=uuid4(), kind="t")


def test_migration_trace_is_written(memory, tmp_path) -> None:
    files = list((tmp_path / "private" / "migrations").glob("*.json"))
    assert len(files) == 1
    trace = json.loads(files[0].read_text(encoding="utf-8"))
    assert trace["facts_added"] > 50 and trace["conflicts_opened"] == 1


# =============================================================== 3.6 minimal context + 3.8 memory tests
def project_names(packet) -> list[str]:
    return packet.ranked_projects


def test_java_letter_puts_quizarena_first_and_invents_nothing(memory) -> None:
    packet = build_context(memory, TaskType.COVER_LETTER, as_of=TODAY, query="Werkstudent Java Backend REST")
    assert project_names(packet)[0] == "QuizArena"
    known = {"QuizArena", "Stream Club", "Data / Machine Learning", "Game Development"}
    assert set(project_names(packet)) <= known


def test_frontend_query_highlights_stream_club(memory) -> None:
    packet = build_context(memory, TaskType.COVER_LETTER, as_of=TODAY, query="Frontend Entwicklung mit React")
    assert project_names(packet)[0] == "Stream Club"


def test_data_query_returns_python_ml_projects(memory) -> None:
    packet = build_context(memory, TaskType.COVER_LETTER, as_of=TODAY, query="Working Student Data Science Python Machine Learning")
    assert project_names(packet)[0] == "Data / Machine Learning"
    assert "skill.pytorch" in packet.keys() and "skill.react" not in packet.keys()


def test_offer_analysis_never_contains_the_full_address_or_legal_data(memory) -> None:
    packet = build_context(memory, TaskType.OFFER_ANALYSIS, as_of=TODAY, query="Werkstudent Java")
    dumped = json.dumps(packet.model_dump(mode="json"), ensure_ascii=False)
    assert "Musterweg" not in dumped and "12345" not in dumped
    assert not any(k.startswith(("legal.", "contact.postal", "contact.email", "contact.phone")) for k in packet.keys())
    assert {"availability.hours_per_week", "preference.domains", "contact.city"} <= packet.keys()


def test_cover_letter_never_contains_residence_permit(memory) -> None:
    packet = build_context(memory, TaskType.COVER_LETTER, as_of=TODAY, query="Java",
                           requested_keys=["legal.residence_permit_valid_until"])
    assert "legal.residence_permit_valid_until" not in packet.keys()
    assert "2027-03-31" not in json.dumps(packet.model_dump(mode="json"))


def test_form_gets_only_requested_values_and_address_needs_confirmation(memory) -> None:
    packet = build_context(memory, TaskType.STANDARD_FORM, as_of=TODAY,
                           requested_keys=["contact.email", "availability.start_date", "contact.postal_address"])
    assert packet.keys() == {"contact.email", "availability.start_date"}
    assert packet.awaiting_user and any("postal_address" in r for r in packet.awaiting_reasons)


def test_form_with_conflicting_or_unknown_value_waits_for_user(memory) -> None:
    packet = build_context(memory, TaskType.STANDARD_FORM, as_of=TODAY, requested_keys=["education.gpa", "skill.kubernetes"])
    assert packet.unknown_keys == ["education.gpa", "skill.kubernetes"] and packet.awaiting_user


def test_every_context_fact_is_traceable(memory) -> None:
    packet = build_context(memory, TaskType.OFFER_ANALYSIS, as_of=TODAY, query="Data")
    assert packet.facts and len(packet.fact_ids) == len(packet.facts)
    assert all(f.source and f.validated_at for f in packet.facts)


def test_github_review_has_no_administrative_data(memory) -> None:
    packet = build_context(memory, TaskType.GITHUB_REVIEW, as_of=TODAY)
    assert all(k.startswith(("project.", "skill.", "identity.github")) for k in packet.keys())


# =============================================================== 3.7 rules + 3.8
def rules(memory) -> ProfileRules:
    return ProfileRules(memory, as_of=TODAY)


def test_work_authorization_after_expiry_waits_for_user(memory) -> None:
    decision = rules(memory).work_authorization(date(2027, 6, 30))
    assert decision.outcome == RuleOutcome.AWAITING_USER


def test_work_authorization_within_validity_is_allowed(memory) -> None:
    assert rules(memory).work_authorization(date(2026, 11, 1)).outcome == RuleOutcome.ALLOW


def test_missing_skill_is_unknown_never_expertise(memory) -> None:
    decision = rules(memory).claim_skill("Rust")
    assert decision.outcome == RuleOutcome.UNKNOWN and decision.value is False


@pytest.mark.parametrize("skill", ["Kubernetes", "AWS", "Terraform", "Spring Boot", "Cloud"])
def test_forbidden_claims_are_blocked(memory, skill: str) -> None:
    assert rules(memory).claim_skill(skill).outcome == RuleOutcome.BLOCK


def test_basic_skill_is_never_turned_into_expertise(memory) -> None:
    r = rules(memory)
    assert r.claim_skill("Docker").value["level"] == "basics"
    assert r.claim_skill("Docker", expert=True).outcome == RuleOutcome.BLOCK


def test_no_invented_cefr_level(memory) -> None:
    r = rules(memory)
    assert r.language_claim("language.allemand", "C1").outcome == RuleOutcome.BLOCK
    assert r.language_claim("language.allemand").outcome == RuleOutcome.ALLOW


def test_hours_salary_availability_bachelor_master(memory) -> None:
    r = rules(memory)
    assert r.weekly_hours(20).outcome == RuleOutcome.ALLOW
    assert r.weekly_hours(30).outcome == RuleOutcome.AWAITING_USER
    assert r.availability().value == date(2026, 11, 1)
    assert r.salary("Werkstudent Softwareentwicklung").value == 15.0
    assert r.salary("Werkstudent Controlling").outcome == RuleOutcome.AWAITING_USER
    assert r.salary("Junior Developer").outcome == RuleOutcome.AWAITING_USER
    assert r.bachelor_end().value == date(2027, 9, 30)
    assert "Master" in r.master_intention().value


def test_address_and_permit_are_never_spontaneous(memory) -> None:
    r = rules(memory)
    assert r.mention_in_document("contact.postal_address", DocumentType.COVER_LETTER).outcome == RuleOutcome.BLOCK
    assert r.mention_in_document("legal.residence_permit_valid_until", DocumentType.CV).outcome == RuleOutcome.BLOCK
    assert r.disclose("legal.residence_permit_valid_until", explicitly_requested=False).outcome == RuleOutcome.BLOCK
    assert r.disclose("contact.postal_address", explicitly_requested=True).outcome == RuleOutcome.AWAITING_USER


def test_location_rules(memory) -> None:
    r = rules(memory)
    assert r.location("Frankfurt am Main").outcome == RuleOutcome.ALLOW
    assert r.location("München").outcome == RuleOutcome.AWAITING_USER
    assert r.location("München", remote_or_hybrid=True).outcome == RuleOutcome.ALLOW


def test_generated_text_is_checked_for_unsupported_claims(memory) -> None:
    letter = (
        "Ich bin AWS-Experte mit Deutsch auf C1-Niveau. Mein Aufenthaltstitel gilt bis 31.03.2027. "
        "Adresse: Musterweg 1, 12345 Musterstadt."
    )
    rules_hit = {v.rule for v in rules(memory).check_text(letter)}
    assert rules_hit >= {"no_invented_cefr", "forbidden_claim", "expertise_claim", "residence_permit_mentioned",
                         "residence_permit_date", "full_address_outside_form"}
    clean = "Mit QuizArena habe ich Java, Vert.x und REST eingesetzt. Ich bin ab dem 1. November 2026 verfügbar."
    assert rules(memory).check_text(clean) == []


def test_success_requires_receipt(memory) -> None:
    submitted = Application(job_offer_id=uuid4(), local_folder=Path("x"), state=ApplicationState.SUBMITTED, submitted_at=NOW)
    assert not ProfileRules.submission_successful(submitted)
    confirmed = Application(
        job_offer_id=uuid4(), local_folder=Path("x"), state=ApplicationState.CONFIRMED, submitted_at=NOW,
        receipt=SubmissionReceipt(kind=ReceiptKind.EMAIL, reference="Eingangsbestätigung"),
    )
    assert ProfileRules.submission_successful(confirmed)


def test_rule_decisions_feed_state_machine_blockers(memory) -> None:
    r = rules(memory)
    blockers = blocking_unknowns([r.weekly_hours(20), r.work_authorization(date(2027, 6, 30)), r.claim_skill("Rust")])
    assert len(blockers) == 2 and "legal.residence_permit_valid_until" in blockers[0]


# =============================================================== phase 3 gate: model sees only the packet
from bewerbungspilot.memory.analysis import build_offer_request, review_model_analysis  # noqa: E402


def test_offer_request_contains_only_the_context_packet(memory) -> None:
    offer = "Fiktive Anzeige: Werkstudent Java Backend, Frankfurt, 20 Std./Woche; Java, REST, SQL."
    packet = build_context(memory, TaskType.OFFER_ANALYSIS, as_of=TODAY, query=offer)
    request = build_offer_request(packet, offer, case_id="B01")
    assert "Musterweg" not in request.prompt and "2027-03-31" not in request.prompt
    assert "alex.beispiel@example.org" not in request.prompt
    assert "QuizArena" in request.prompt


def test_model_answer_is_checked_by_code(memory) -> None:
    packet = build_context(memory, TaskType.OFFER_ANALYSIS, as_of=TODAY, query="Java")
    answer = {
        "case_id": "B01", "score": 0, "decision": "APPLY",
        "factual_matches": ["Java via QuizArena"], "gaps": [], "blockers": ["Vollzeit"],
        "missing_information": [], "recommended_projects": ["QuizArena", "Kubernetes Platform"],
        "explanation": "Kandidat hat AWS Erfahrung und Deutsch C1.",
    }
    issues = review_model_analysis(answer, packet, ProfileRules(memory, as_of=TODAY))
    joined = " | ".join(issues)
    for expected in ("Kubernetes Platform", "score nul", "APPLY malgré", "forbidden_claim", "no_invented_cefr"):
        assert expected in joined


def test_incoherent_answer_seen_on_windows_is_flagged(memory) -> None:
    """Real gpt-oss answer of 26/09/2026: REJECT, score 1, yet 'meets all requirements'."""
    packet = build_context(memory, TaskType.OFFER_ANALYSIS, as_of=TODAY, query="Werkstudent Java Backend")
    answer = {
        "case_id": "B01", "score": 1, "decision": "REJECT",
        "factual_matches": ["skill.java", "skill.sql", "project.quizarena", "availability.hours_per_week"],
        "gaps": ["education.institution", "language.allemand"], "blockers": [], "missing_information": [],
        "recommended_projects": ["QuizArena"],
        "explanation": "The candidate meets all technical and logistical requirements of the offer.",
    }
    joined = " | ".join(review_model_analysis(answer, packet, ProfileRules(memory, as_of=TODAY)))
    assert "REJECT sans blocage" in joined
    assert "score 1 incohérent" in joined
    assert "faits pourtant fournis" in joined
