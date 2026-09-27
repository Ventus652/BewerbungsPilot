"""Phase 4.3 — validated CV library, PDF rendering and automatic visual checks."""

import json
from datetime import date
from pathlib import Path

import pytest

from bewerbungspilot.documents.dossier import create_dossier
from bewerbungspilot.documents.pdf import (
    LayoutOverflowError,
    LetterHeader,
    choose_base_cv,
    render_cv,
    render_letter,
    reorder_projects,
    verify_pdf,
)
from bewerbungspilot.documents.render import cv_truth_issues, render_documents
from bewerbungspilot.domain import ApplicationState as S, DocumentType, VisualValidationStatus
from bewerbungspilot.domain.enums import FactCategory
from bewerbungspilot.jobs.evaluation import evaluate_offer
from bewerbungspilot.jobs.extraction import OfferExtraction, verify_extraction
from bewerbungspilot.memory.benchmark import _fact, memory_from_benchmark_profile
from bewerbungspilot.memory.cv_library import CvEntry, build_library, parse_generator
from bewerbungspilot.memory.records import MemorySnapshot
from bewerbungspilot.memory.rules import ProfileRules

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "memory_root"
GENERATOR = FIXTURES / "2026-01-10_Demo_GmbH_Werkstudent_Software" / "generer_documents.py"
BENCH = Path(__file__).resolve().parents[2] / "benchmarks"
TODAY = date(2026, 9, 27)
MEMORY = MemorySnapshot(facts=[
    *memory_from_benchmark_profile().facts,
    _fact("identity.display_name", "Alex Beispiel", FactCategory.IDENTITY),
    _fact("contact.city", "Musterstadt, Allemagne", FactCategory.CONTACT),
    _fact("contact.email", "alex.beispiel@example.org", FactCategory.CONTACT),
    _fact("education.institution", "Hochschule Musterstadt (HSM)", FactCategory.EDUCATION),
    _fact("education.program", "Informatik", FactCategory.EDUCATION),
    _fact("education.bachelor_expected_end", "2027-09-30", FactCategory.EDUCATION),
])
LIBRARY = build_library(FIXTURES)


def test_generator_is_parsed_without_being_executed(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    document = parse_generator(GENERATOR, FIXTURES)
    assert not (tmp_path / "THIS_FILE_MUST_NEVER_BE_CREATED.txt").exists()
    assert document.language == "de"
    assert [s.title for s in document.sections] == ["Studium", "Ausgewählte Projekte", "Technische Kompetenzen", "Sprachen"]
    assert document.header[0].text == "Alex Beispiel"
    assert '<link href="mailto:alex.beispiel@example.org">' in document.header[2].text
    items = [e for e in document.sections[1].entries if e.kind == "item"]
    assert items[0].date == "2025 - 2026" and items[1].date is None


def test_unsupported_claim_in_a_validated_cv_is_detected() -> None:
    document = LIBRARY[0]
    tainted = document.model_copy(update={"sections": [
        *document.sections[:-1],
        document.sections[-1].model_copy(update={"entries": [CvEntry(kind="para", text="Grundlagen in Node.js", line=1)]}),
    ]})
    rules = ProfileRules(MEMORY, as_of=TODAY)
    assert cv_truth_issues(document, MEMORY, rules) == []
    assert "Node.js" in cv_truth_issues(tainted, MEMORY, rules)


def test_projects_are_reordered_without_changing_text() -> None:
    document = LIBRARY[0]
    reordered = reorder_projects(document, ["Stream Club", "Data / Machine Learning", "QuizArena"])
    titles = [e.text for e in reordered.sections[1].entries if e.kind == "item"]
    assert titles == ["Stream Club · Frontend-Prototyp", "Datenanalyse und Machine Learning", "QuizArena · Multiplayer-Quizplattform"]
    assert sorted(e.text for e in reordered.sections[1].entries) == sorted(e.text for e in document.sections[1].entries)


def test_base_cv_matches_language() -> None:
    assert choose_base_cv(LIBRARY, "de", {"java"}).source.endswith("generer_documents.py")


def test_rendered_cv_is_one_page_with_embedded_fonts(tmp_path) -> None:
    out = render_cv(reorder_projects(LIBRARY[0], ["Stream Club"]), tmp_path / "cv.pdf", title="t", subject="s")
    check = verify_pdf(out, must_contain=["Alex Beispiel", "Stream Club"], ordered=["Stream Club", "QuizArena"])
    assert check.ok, check.issues
    assert check.pages == 1 and check.fonts_embedded


def test_verification_catches_wrong_order_and_foreign_company(tmp_path) -> None:
    out = render_cv(LIBRARY[0], tmp_path / "cv.pdf", title="t", subject="s")
    check = verify_pdf(out, must_contain=["Alex Beispiel"], ordered=["Stream Club", "QuizArena"], must_not_contain=["Musterstadt"])
    assert not check.ok
    assert any("ordre" in i for i in check.issues) and any("interdit" in i for i in check.issues)


def test_overflowing_content_is_refused(tmp_path) -> None:
    document = LIBRARY[0]
    long_section = document.sections[1].model_copy(update={"entries": document.sections[1].entries * 12})
    with pytest.raises(LayoutOverflowError):
        render_cv(document.model_copy(update={"sections": [long_section]}), tmp_path / "cv.pdf", title="t", subject="s")


def test_letter_pdf_contains_recipient_and_no_street_address(tmp_path) -> None:
    markdown = ("# Bewerbung als Werkstudent\n\nSehr geehrte Damen und Herren,\n\nAbsatz eins mit genug Text, um eine echte Zeile zu füllen und die Extraktion zu prüfen.\n\n"
                "Absatz zwei mit weiterem Text über QuizArena, Java und SQL im Studium und in Projekten.\n\n"
                "Absatz drei zur Verfügbarkeit ab dem 15. Oktober 2026 für 20 Stunden pro Woche.\n\n"
                "Mit freundlichen Grüßen\n\nAlex Beispiel\n")
    header = LetterHeader(name="Alex Beispiel", city="Musterstadt", email="alex.beispiel@example.org",
                          recipient=["Demo GmbH", "Frankfurt"], language="de", on=TODAY)
    out = render_letter(markdown, header, tmp_path / "letter.pdf", title="t")
    check = verify_pdf(out, must_contain=["Demo GmbH", "Musterstadt, 27. September 2026", "Mit freundlichen Grüßen"],
                       must_not_contain=["Musterweg"])
    assert check.ok, check.issues


def _dossier(tmp_path: Path):
    text = json.loads((BENCH / "cases" / "A01.json").read_text(encoding="utf-8"))["input"]
    extraction = OfferExtraction.model_validate(json.loads((BENCH / "expected_reviewed" / "A01.json").read_text(encoding="utf-8")))
    verified = verify_extraction(extraction, text)
    evaluation = evaluate_offer(verified, text, MEMORY, as_of=TODAY)
    return create_dossier(offer_text=text, verified=verified, evaluation=evaluation, memory=MEMORY, root=tmp_path, as_of=TODAY)


def test_dossier_documents_are_rendered_verified_and_prepared(tmp_path) -> None:
    dossier = _dossier(tmp_path)
    result = render_documents(dossier.folder, memory=MEMORY, library=LIBRARY, as_of=TODAY, past_companies=["Pixelpfad AG"])
    assert result.cv.ok and result.letter.ok, (result.cv.issues, result.letter.issues)
    assert result.application.state == S.DOCUMENTS_PREPARED and result.transition is not None
    assert {d.document_type for d in result.documents} == {DocumentType.CV, DocumentType.COVER_LETTER}
    assert all(d.visual_validation == VisualValidationStatus.PASSED for d in result.documents)
    assert sorted(result.application.document_ids) == sorted(d.id for d in result.documents)
    assert all((dossier.folder / d.path).exists() for d in result.documents)


def test_rerender_keeps_state_and_replaces_documents(tmp_path) -> None:
    dossier = _dossier(tmp_path)
    first = render_documents(dossier.folder, memory=MEMORY, library=LIBRARY, as_of=TODAY)
    second = render_documents(dossier.folder, memory=MEMORY, library=LIBRARY, as_of=TODAY)
    assert second.transition is None and second.application.state == S.DOCUMENTS_PREPARED
    assert set(second.application.document_ids) == {d.id for d in second.documents} != {d.id for d in first.documents}
    assert len(list(dossier.folder.glob("*.pdf"))) == 2


def test_edited_letter_with_invented_claim_is_never_marked_valid(tmp_path) -> None:
    dossier = _dossier(tmp_path)
    letter = dossier.folder / "Motivationsschreiben.md"
    letter.write_text(letter.read_text(encoding="utf-8").replace("Über die Einladung", "Ich bin AWS-Experte. Über die Einladung"), encoding="utf-8")
    result = render_documents(dossier.folder, memory=MEMORY, library=LIBRARY, as_of=TODAY)
    assert not result.letter.ok and result.application.state == S.SELECTED
    assert any(d.visual_validation == VisualValidationStatus.FAILED for d in result.documents)
