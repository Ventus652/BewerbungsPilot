"""Phase 4.1 — the model extracts, the code judges."""

import json
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from bewerbungspilot.domain import (
    Application,
    ApplicationState as S,
    ApplicationStateMachine,
    MatchDecision,
    TransitionActor,
    TransitionCommand,
    TransitionContext,
)
from bewerbungspilot.jobs.evaluation import evaluate_offer
from bewerbungspilot.jobs.extraction import OfferExtraction, heuristic_facts, verify_extraction
from bewerbungspilot.memory.benchmark import memory_from_benchmark_profile

BENCH = Path(__file__).resolve().parents[2] / "benchmarks"
TODAY = date(2026, 9, 26)
MEMORY = memory_from_benchmark_profile()

# Reference extractions for the B cases (the A cases use benchmarks/expected_reviewed).
B_EXTRACTIONS = {
    "B01": dict(company="Softwerk Mitte GmbH", title="Werkstudent Java Backend", location="Gießen/hybrid",
                employment_type="Werkstudent", weekly_hours="16–20", technologies_required=["Java", "REST", "SQL"],
                technologies_mentioned=["Vert.x"], evidence_fragments=["16–20 Std./Woche", "Java, REST, SQL erforderlich"]),
    "B02": dict(title="Werkstudent Data Engineering", location="Frankfurt/hybrid", employment_type="Werkstudent",
                technologies_required=["Python", "SQL"], technologies_mentioned=["Cloud-ETL"],
                evidence_fragments=["Python und SQL erforderlich"]),
    "B03": dict(title="Senior .NET Engineer", location="Hamburg", employment_type="Vollzeit", weekly_hours="40",
                technologies_required=["C#/.NET"], evidence_fragments=["keine Teilzeit oder Werkstudentenverträge"]),
}
EXPECTED = {  # decision, score band
    "A01": (MatchDecision.APPLY, 80, 100),
    "A02": (MatchDecision.APPLY, 80, 100),
    "A03": (MatchDecision.APPLY, 80, 100),
    "A04": (MatchDecision.REJECT, 0, 30),
    "B01": (MatchDecision.APPLY, 80, 100),
    "B02": (MatchDecision.REVIEW, 45, 79),
    "B03": (MatchDecision.REJECT, 0, 30),
}


def offer_text(case: str) -> str:
    return json.loads((BENCH / "cases" / f"{case}.json").read_text(encoding="utf-8"))["input"]


def reference_extraction(case: str) -> OfferExtraction:
    if case.startswith("A"):
        return OfferExtraction.model_validate(json.loads((BENCH / "expected_reviewed" / f"{case}.json").read_text(encoding="utf-8")))
    return OfferExtraction(case_id=case, **B_EXTRACTIONS[case])


def evaluate(case: str, extraction: OfferExtraction | None = None):
    text = offer_text(case)
    return evaluate_offer(verify_extraction(extraction or reference_extraction(case), text), text, MEMORY, as_of=TODAY)


@pytest.mark.parametrize("case", sorted(EXPECTED))
def test_decision_and_score_band_match_the_reviewed_benchmark(case: str) -> None:
    decision, low, high = EXPECTED[case]
    assessment = evaluate(case).assessment
    assert assessment.decision == decision
    assert low <= assessment.score <= high


def test_b01_reasons_cover_the_expected_points() -> None:
    evaluation = evaluate("B01")
    statements = " | ".join(i.statement for i in evaluation.assessment.matches)
    for expected in ("Heures compatibles", "Lieu accepté : Gießen", "Java", "REST", "SQL"):
        assert expected in statements
    assert evaluation.assessment.recommended_projects[0] == "QuizArena"


def test_b02_flags_hours_start_and_cloud_etl() -> None:
    a = evaluate("B02").assessment
    unknowns = " | ".join(i.statement for i in a.unknowns)
    assert "heures" in unknowns.lower() and "début" in unknowns.lower()
    assert any("Cloud-ETL" in g.statement for g in a.gaps)


def test_b03_lists_every_blocker() -> None:
    blockers = " | ".join(i.statement for i in evaluate("B03").assessment.blockers)
    for expected in ("40 h", "Hamburg", "Werkstudent", "C#/.NET"):
        assert expected in blockers


@pytest.mark.parametrize("case", sorted(EXPECTED))
def test_forbidden_claims_never_appear_as_matches(case: str) -> None:
    matches = " ".join(i.statement for i in evaluate(case).assessment.matches)
    for term in ("Spring Boot", "AWS", "Kubernetes", "Terraform", "C#/.NET", "Cloud", "DevOps"):
        assert term not in matches


@pytest.mark.parametrize("case", sorted(EXPECTED))
def test_every_item_is_traceable_to_offer_and_memory(case: str) -> None:
    a = evaluate(case).assessment
    for item in a.matches + a.gaps + a.blockers:
        assert any(ref.startswith("offer:") for ref in item.source_refs)
    for item in a.matches:
        assert any(ref.startswith("fact:") for ref in item.source_refs)


def test_invented_technology_and_quote_are_removed_before_judging() -> None:
    tampered = reference_extraction("B01").model_copy(update={
        "technologies_required": ["Java", "REST", "SQL", "Spring Boot"],
        "evidence_fragments": ["16–20 Std./Woche", "5 Jahre Erfahrung mit Spring Boot"],
        "publication_date": "2026-09-01",
    })
    verified = verify_extraction(tampered, offer_text("B01"))
    assert "Spring Boot" not in verified.extraction.technologies_required
    assert len(verified.removed) == 3 and verified.extraction.publication_date is None
    evaluation = evaluate_offer(verified, offer_text("B01"), MEMORY, as_of=TODAY)
    assert evaluation.assessment.decision == MatchDecision.APPLY
    assert evaluation.removed_from_extraction


def test_model_and_rules_disagreeing_on_hours_becomes_an_unknown() -> None:
    wrong = reference_extraction("B01").model_copy(update={"weekly_hours": "30"})
    a = evaluate("B01", wrong).assessment
    assert a.decision == MatchDecision.REVIEW
    assert any("incohérentes" in u.statement for u in a.unknowns)


def test_relative_date_is_never_made_absolute() -> None:
    verified = verify_extraction(reference_extraction("A03").model_copy(update={"publication_date": "2026-09-24"}), offer_text("A03"))
    assert verified.extraction.publication_date is None and "publication_date" in verified.unknowns


def test_the_model_score_is_ignored() -> None:
    """26/09/2026: gpt-oss answered REJECT/1 for B01. Only the extraction is used now."""
    model_like = reference_extraction("B01").model_copy(update={"hard_requirements": ["REJECT", "score 1"]})
    assert evaluate("B01", model_like).assessment.decision == MatchDecision.APPLY


def test_heuristics_read_contract_signals() -> None:
    b03 = heuristic_facts(offer_text("B03"))
    assert b03.full_time and b03.no_student_contract and b03.onsite_only and b03.min_years_experience == 5
    a01 = heuristic_facts(offer_text("A01"))
    assert (a01.hours_min, a01.hours_max) == (16, 20) and a01.start_date == date(2026, 10, 15) and a01.enrollment_required


@pytest.mark.parametrize(("case", "target"), [("B01", S.SELECTED), ("B02", S.AWAITING_USER), ("B03", S.REJECTED)])
def test_decision_drives_the_state_machine(case: str, target: S) -> None:
    offer_id = uuid4()
    text = offer_text(case)
    evaluation = evaluate_offer(verify_extraction(reference_extraction(case), text), text, MEMORY, as_of=TODAY, job_offer_id=offer_id)
    assert evaluation.next_state == target
    machine = ApplicationStateMachine()
    app = Application(job_offer_id=offer_id, local_folder=Path("data/generated/x"),
                      created_at=datetime(2026, 9, 26, tzinfo=timezone.utc), updated_at=datetime(2026, 9, 26, tzinfo=timezone.utc))
    now = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
    ctx = TransitionContext(assessment=evaluation.assessment, awaiting_reason="Évaluation REVIEW : vérifier les inconnues")
    app, _ = machine.apply(app, TransitionCommand(application_id=app.id, expected_state=S.DISCOVERED, expected_version=0,
                                                  target_state=S.EVALUATED, reason="évalué", actor=TransitionActor.SYSTEM,
                                                  requested_at=now, context=ctx))
    app, _ = machine.apply(app, TransitionCommand(application_id=app.id, expected_state=S.EVALUATED, expected_version=1,
                                                  target_state=target, reason="décision du moteur", actor=TransitionActor.SYSTEM,
                                                  requested_at=now, context=ctx))
    assert app.state == target


def test_quoted_citations_and_german_dates_from_the_real_run_are_accepted() -> None:
    """27/09/2026 Windows run: gpt-oss wrapped quotes and wrote 22.09.2026."""
    extraction = reference_extraction("A01").model_copy(update={
        "publication_date": "22.09.2026",
        "evidence_fragments": ['"Kenntnisse in Java und SQL erforderlich"', "„16–20 Std./Woche“"],
    })
    verified = verify_extraction(extraction, offer_text("A01"))
    assert verified.removed == []
    assert verified.extraction.publication_date == "2026-09-22"


def test_invented_date_from_relative_text_is_removed() -> None:
    """Same run: 'vor 2 Tagen' became '2024-09-25'."""
    verified = verify_extraction(reference_extraction("A03").model_copy(update={"publication_date": "2024-09-25"}), offer_text("A03"))
    assert verified.extraction.publication_date is None and verified.removed


def test_model_schema_accepts_german_dates_but_benchmark_schema_is_untouched() -> None:
    from bewerbungspilot.jobs.extraction import SCHEMA_PATH, load_schema
    assert "pattern" not in load_schema()["properties"]["publication_date"]
    assert "pattern" in json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))["properties"]["publication_date"]


# --------------------------------------------------------------- 27/09/2026 live run: "at least one of" tracks
ANY_OF_OFFER = (
    "Werkstudent Software Developer (m/w/d) in Darmstadt. Laufendes Studium der Informatik. "
    "Du hast Erfahrung in mindestens einem der folgenden Bereiche: iOS (Swift, UIKit), Frontend (HTML, CSS, JavaScript), "
    "Webanwendungen (PHP, MySQL, JavaScript) oder Cloud/DevOps (Kubernetes, Docker, TypeScript). "
    "Gute Deutsch- und Englischkenntnisse."
)


def _any_of_extraction(**update) -> OfferExtraction:
    base = dict(company="Demo Shop GmbH", title="Werkstudent Software Developer (m/w/d)", location="Darmstadt",
                employment_type="Werkstudent",
                technologies_required=["Swift", "UIKit", "HTML", "CSS", "JavaScript", "PHP", "MySQL", "Kubernetes", "Docker", "TypeScript"])
    base.update(update)
    return OfferExtraction(**base)


def test_flattened_alternative_tracks_are_not_blockers() -> None:
    """The live run rejected such an offer (30/100) because Kubernetes was read as mandatory."""
    verified = verify_extraction(_any_of_extraction(), ANY_OF_OFFER)
    evaluation = evaluate_offer(verified, ANY_OF_OFFER, MEMORY, as_of=TODAY)
    assert evaluation.assessment.decision != MatchDecision.REJECT
    assert not evaluation.assessment.blockers
    assert any("Exigences alternatives" in n for n in evaluation.notes)


def test_explicit_tracks_pick_the_best_covered_one() -> None:
    extraction = _any_of_extraction(technologies_required=[], alternative_requirement_groups=[
        ["Swift", "UIKit"], ["HTML", "CSS", "JavaScript"], ["PHP", "MySQL", "JavaScript"], ["Kubernetes", "Docker", "TypeScript"]])
    verified = verify_extraction(extraction, ANY_OF_OFFER)
    a = evaluate_offer(verified, ANY_OF_OFFER, MEMORY, as_of=TODAY).assessment
    assert not a.blockers
    covered = [m.statement for m in a.matches if m.statement.startswith("Piste exigée couverte")]
    assert covered and all("HTML / CSS / JavaScript" in s for s in covered)
    assert not any("Kubernetes" in m.statement for m in a.matches)


def test_required_forbidden_technology_outside_tracks_still_blocks() -> None:
    assert evaluate("A04").assessment.decision == MatchDecision.REJECT


def test_tracks_written_with_their_technologies_in_parentheses() -> None:
    """Regression: a model may return each alternative track as 'Label (tech, tech)'."""
    extraction = _any_of_extraction(
        technologies_required=[],
        technologies_mentioned=["Vue.js", "PHP/Symfony", "ERP", "Versand", "Kassensysteme"],
        alternative_requirement_groups=[["iOS (Swift, UIKit)", "Frontend (HTML, CSS, JavaScript)",
                                         "Webanwendungen (PHP, MySQL, JavaScript)", "Cloud/DevOps (Kubernetes, Docker, TypeScript)"]])
    text = ANY_OF_OFFER + " Features im Frontend (Vue.js) und Backend (PHP/Symfony) für ERP-, Versand- und Kassensysteme."
    a = evaluate_offer(verify_extraction(extraction, text), text, MEMORY, as_of=TODAY).assessment
    assert any(m.statement.startswith("Piste exigée couverte (HTML / CSS / JavaScript)") for m in a.matches)
    gaps = " ".join(g.statement for g in a.gaps)
    assert "ERP" not in gaps and "Versand" not in gaps and "Kassensysteme" not in gaps
    assert "Vue.js" in gaps
