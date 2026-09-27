"""Phase 4.2 — constrained letter, letter checks, CV plan, isolated dossier."""

import json
from datetime import date
from pathlib import Path

import pytest

from bewerbungspilot.documents.checks import check_letter
from bewerbungspilot.documents.cv_plan import build_cv_plan
from bewerbungspilot.documents.dossier import DuplicateOfferError, RejectedOfferError, create_dossier
from bewerbungspilot.documents.letters import build_letter, clean_title, offer_language, with_article
from bewerbungspilot.domain import ApplicationState as S
from bewerbungspilot.jobs.evaluation import evaluate_offer
from bewerbungspilot.jobs.extraction import OfferExtraction, verify_extraction
from bewerbungspilot.memory.benchmark import memory_from_benchmark_profile
from bewerbungspilot.memory.records import MemorySnapshot
from bewerbungspilot.memory.rules import ProfileRules
from bewerbungspilot.memory.benchmark import _fact
from bewerbungspilot.domain.enums import FactCategory

BENCH = Path(__file__).resolve().parents[2] / "benchmarks"
TODAY = date(2026, 9, 27)
BASE = memory_from_benchmark_profile()
MEMORY = MemorySnapshot(facts=[*BASE.facts, _fact("identity.display_name", "Alex Beispiel", FactCategory.IDENTITY),
                               _fact("education.institution", "Hochschule Musterstadt (HSM)", FactCategory.EDUCATION),
                               _fact("education.program", "Informatik", FactCategory.EDUCATION),
                               _fact("education.bachelor_expected_end", "2027-09-30", FactCategory.EDUCATION),
                               _fact("education.master_intention", "poursuivre avec un Master", FactCategory.EDUCATION)])
RULES = ProfileRules(MEMORY, as_of=TODAY)
ENGLISH_OFFER = ("Nordlicht Software GmbH is looking for a Working Student Backend (f/m/d) in Frankfurt, hybrid. "
                 "16-20 hours per week. You bring Java and SQL skills; experience with Spring Boot is a plus. "
                 "Start from 15.10.2026.")


def case(case_id: str):
    text = json.loads((BENCH / "cases" / f"{case_id}.json").read_text(encoding="utf-8"))["input"]
    extraction = OfferExtraction.model_validate(json.loads((BENCH / "expected_reviewed" / f"{case_id}.json").read_text(encoding="utf-8")))
    verified = verify_extraction(extraction, text)
    return text, verified, evaluate_offer(verified, text, MEMORY, as_of=TODAY)


def english_case():
    extraction = OfferExtraction(company="Nordlicht Software GmbH", title="Working Student Backend (f/m/d)", location="Frankfurt",
                                 weekly_hours="16-20", technologies_required=["Java", "SQL"], technologies_mentioned=["Spring Boot"])
    verified = verify_extraction(extraction, ENGLISH_OFFER)
    return ENGLISH_OFFER, verified, evaluate_offer(verified, ENGLISH_OFFER, MEMORY, as_of=TODAY)


def check(text: str, offer: str, company: str | None = "Softwerk Mitte GmbH", numbers: str = "15. Oktober 2026 20 Stunden September 2027", others=()):
    return check_letter(text, memory=MEMORY, rules=RULES, offer_text=offer, company=company,
                        allowed_numbers_text=numbers, other_companies=others, signature="Alex Beispiel")


def letter_for(case_id: str, **kwargs):
    text, verified, evaluation = case(case_id)
    return text, verified, evaluation, build_letter(MEMORY, evaluation, verified.extraction, text, as_of=TODAY, **kwargs)


# ------------------------------------------------------------------ generated letters pass the checks
@pytest.mark.parametrize("case_id", ["A01", "A02", "A03"])
def test_generated_letter_passes_every_check(case_id: str) -> None:
    offer, verified, _, letter = letter_for(case_id)
    result = check(letter.text, offer, verified.extraction.company, letter.allowed_numbers_text, ["Pixelpfad AG", "Softwerk Mitte GmbH", "AnalyseWerk UG"])
    assert result.ok, result.issues


def test_letter_uses_facts_and_offer_only() -> None:
    _, _, _, letter = letter_for("A01")
    assert "der Softwerk Mitte GmbH" in letter.text and "15. Oktober 2026" in letter.text and "20 Stunden" in letter.text
    assert "QuizArena" in letter.text and "an der HSM" in letter.text
    assert "Spring" not in letter.text and "(m/w/d)" not in letter.text.split("\n\n", 2)[2]
    assert letter.text.rstrip().endswith("Mit freundlichen Grüßen\n\nAlex Beispiel")


def test_missing_preferred_technology_is_only_something_to_learn() -> None:
    offer, verified, evaluation = english_case()
    letter = build_letter(MEMORY, evaluation, verified.extraction, offer, as_of=TODAY)
    assert letter.language == "en"
    assert "get to know Spring Boot" in letter.text
    assert check(letter.text, offer, "Nordlicht Software GmbH", letter.allowed_numbers_text).ok


def test_salary_only_when_requested_and_validated() -> None:
    assert "Gehaltsvorstellung" not in letter_for("A01")[3].text


def test_title_and_company_wording() -> None:
    assert clean_title("Werkstudent:in Frontend (m/w/d)") == "Werkstudent Frontend"
    assert with_article("Pixelpfad AG") == "der Pixelpfad AG" and with_article("Acme") == "Acme"
    assert offer_language(ENGLISH_OFFER) == "en" and offer_language("Wir suchen einen Werkstudent mit Java und SQL") == "de"


# ------------------------------------------------------------------ the checker catches what models did in the benchmark
GOOD = ("# Bewerbung\n\nSehr geehrte Damen und Herren,\n\n{p1}\n\nIn meinem Projekt QuizArena habe ich mit Java gearbeitet.\n\n"
        "Ab dem 15. Oktober 2026 stehe ich für 20 Stunden pro Woche zur Verfügung.\n\n"
        "Mit freundlichen Grüßen\n\nAlex Beispiel\n")
OFFER = "Werkstudent Java Backend, Gießen. Java und SQL erforderlich; Spring Boot von Vorteil."


def test_reference_letter_is_accepted() -> None:
    assert check(GOOD.format(p1="ich bewerbe mich als Werkstudent Java Backend."), OFFER).ok


@pytest.mark.parametrize(("sentence", "rule"), [
    ("Ich habe Spring Boot in mehreren Projekten eingesetzt.", "forbidden_claim"),
    ("Ich bringe Erfahrung mit Angular mit.", "undocumented_technology"),
    ("Angular möchte ich gern kennenlernen.", "undocumented_technology"),  # not in the offer
    ("Ich habe 3 Jahre Berufserfahrung.", "unsupported_number"),
    ("Mein Deutsch entspricht dem Niveau C1.", "no_invented_cefr"),
    ("Mein Aufenthaltstitel ist gültig.", "residence_permit_mentioned"),
    ("Ich bin Java-Experte.", "expertise_claim"),
    ("Bei Pixelpfad AG habe ich gelernt, sauber zu arbeiten.", "other_company_named"),
])
def test_checker_rejects_unsupported_claims(sentence: str, rule: str) -> None:
    result = check(GOOD.format(p1=sentence), OFFER, others=["Pixelpfad AG"])
    assert rule in {i.rule for i in result.issues}, result.issues


def test_learning_an_offer_technology_is_allowed() -> None:
    assert check(GOOD.format(p1="Spring Boot möchte ich in Ihrem Team gerne kennenlernen."), OFFER).ok  # learning, not claiming
    assert check(GOOD.format(p1="SQL vertiefe ich gerade."), OFFER).ok


@pytest.mark.parametrize(("text", "rule"), [
    ("Sehr geehrte Damen und Herren, ich bewerbe mich. Ich habe Java gelernt. Mit freundlichen Grüßen Alex Beispiel", "format_paragraphs"),
    (GOOD.format(p1="x").replace("Mit freundlichen Grüßen\n\nAlex Beispiel", "Mit freundlichen Grüßen\nAlex Beispiel"), "format_signature"),
    (GOOD.format(p1="\tich bewerbe mich."), "format_indentation"),
    (GOOD.format(p1="ich bewerbe mich.").replace("Herren,", "Herren"), "format_salutation"),
])
def test_email_formatting_rule_is_enforced(text: str, rule: str) -> None:
    assert rule in {i.rule for i in check(text, OFFER).issues}


# ------------------------------------------------------------------ CV plan
def test_cv_plan_reorders_without_inventing() -> None:
    offer, _, evaluation = case("A02")
    plan = build_cv_plan(MEMORY, evaluation, offer)
    assert plan.project_order[0] == "Stream Club"
    assert set(plan.skills_highlighted) <= {f.value["name"] for f in MEMORY.facts if f.key.startswith("skill.")}
    assert "Docker" in plan.skills_basics and not plan.show_gpa


# ------------------------------------------------------------------ dossier
def test_dossier_is_complete_isolated_and_selected(tmp_path: Path) -> None:
    offer, verified, evaluation = case("A01")
    result = create_dossier(offer_text=offer, verified=verified, evaluation=evaluation, memory=MEMORY, root=tmp_path, as_of=TODAY)
    names = {p.name for p in result.folder.iterdir()}
    assert {"README.md", "Stellenanzeige.md", "Motivationsschreiben.md", "evaluation.json", "extraction.json", "fact_pack.json",
            "letter_check.json", "cv_plan.json", "application.json", "transitions.json", "meta.json"} <= names
    assert result.application.state == S.SELECTED and result.letter_check.ok
    assert [r.to_state for r in result.transitions] == [S.EVALUATED, S.SELECTED]
    assert "Softwerk Mitte" in (result.folder / "README.md").read_text(encoding="utf-8")


def test_same_offer_twice_is_refused(tmp_path: Path) -> None:
    offer, verified, evaluation = case("A01")
    create_dossier(offer_text=offer, verified=verified, evaluation=evaluation, memory=MEMORY, root=tmp_path, as_of=TODAY)
    with pytest.raises(DuplicateOfferError):
        create_dossier(offer_text=offer, verified=verified, evaluation=evaluation, memory=MEMORY, root=tmp_path, as_of=TODAY)


def test_offer_already_applied_in_the_past_is_refused(tmp_path: Path) -> None:
    offer, verified, evaluation = case("A02")
    past = [{"company": "Pixelpfad AG", "position": "Werkstudent:in Frontend (React/TypeScript)", "location": "Frankfurt am Main (hybrid)", "folder": "old"}]
    with pytest.raises(DuplicateOfferError):
        create_dossier(offer_text=offer, verified=verified, evaluation=evaluation, memory=MEMORY, root=tmp_path, as_of=TODAY, past_applications=past)


def test_rejected_offer_gets_no_dossier(tmp_path: Path) -> None:
    offer, verified, evaluation = case("A04")
    with pytest.raises(RejectedOfferError):
        create_dossier(offer_text=offer, verified=verified, evaluation=evaluation, memory=MEMORY, root=tmp_path, as_of=TODAY)
    assert not any(tmp_path.iterdir())


def test_offer_without_company_gets_no_dossier(tmp_path: Path) -> None:
    """27/09/2026 live run: B02 names no employer; no dossier may be guessed."""
    from bewerbungspilot.core.errors import BewerbungspilotError
    offer, verified, evaluation = case("A02")
    anonymous = verified.model_copy(update={"extraction": verified.extraction.model_copy(update={"company": None})})
    with pytest.raises(BewerbungspilotError):
        create_dossier(offer_text=offer, verified=anonymous, evaluation=evaluation, memory=MEMORY, root=tmp_path, as_of=TODAY)
    assert not any(tmp_path.iterdir())



def test_letter_uses_the_covered_alternative_track_and_normal_city_case() -> None:
    """Regression: no technology cited from the covered track and an all-caps city."""
    offer = ("Werkstudent Software Developer (m/w/d) in DARMSTADT. 20 Stunden pro Woche. "
             "Du hast Erfahrung in mindestens einem der folgenden Bereiche: iOS (Swift, UIKit), Frontend (HTML, CSS, JavaScript) "
             "oder Cloud/DevOps (Kubernetes, Docker, TypeScript). Erfahrung mit ChatGPT ist ein Plus.")
    extraction = OfferExtraction(company="Demo Shop GmbH", title="Werkstudent Software Developer (m/w/d)", location="DARMSTADT",
                                 employment_type="Werkstudent", technologies_mentioned=["ChatGPT"],
                                 alternative_requirement_groups=[["Swift", "UIKit"], ["HTML", "CSS", "JavaScript"],
                                                                 ["Kubernetes", "Docker", "TypeScript"]])
    verified = verify_extraction(extraction, offer)
    evaluation = evaluate_offer(verified, offer, MEMORY, as_of=TODAY)
    letter = build_letter(MEMORY, evaluation, verified.extraction, offer, as_of=TODAY)
    first = letter.text.split("\n\n")[2]
    assert "HTML" in first and "JavaScript" in first
    assert "DARMSTADT" not in letter.text
    assert "ChatGPT" not in letter.text  # AI-tool usage is asked to the user, never "to discover"
    assert letter.text.count("HTML") == 1
