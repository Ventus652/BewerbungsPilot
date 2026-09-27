"""Phase 5.1 — real browser adapter on a local Personio-like page (skipped without Playwright)."""

import json
from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

from bewerbungspilot.api.dossiers import apply_user_action, save_user_answers  # noqa: E402
from bewerbungspilot.browser.portal import PortalError  # noqa: E402
from bewerbungspilot.browser.runner import authorize_submission, fill_application, submit_application  # noqa: E402
from bewerbungspilot.browser.web_portal import WebPortal, personio_apply_url  # noqa: E402
from bewerbungspilot.documents.dossier import create_dossier  # noqa: E402
from bewerbungspilot.documents.render import render_documents  # noqa: E402
from bewerbungspilot.domain import ApplicationState as S  # noqa: E402
from bewerbungspilot.domain.enums import FactCategory  # noqa: E402
from bewerbungspilot.jobs.evaluation import evaluate_offer  # noqa: E402
from bewerbungspilot.jobs.extraction import OfferExtraction, verify_extraction  # noqa: E402
from bewerbungspilot.memory.benchmark import _fact, memory_from_benchmark_profile  # noqa: E402
from bewerbungspilot.memory.cv_library import build_library  # noqa: E402
from bewerbungspilot.memory.records import MemorySnapshot  # noqa: E402

HERE = Path(__file__).resolve().parents[1]
PAGE = (HERE / "fixtures" / "portals" / "personio_like.html").as_uri()
BENCH = HERE.parent / "benchmarks"
TODAY = date(2026, 9, 27)
MEMORY = MemorySnapshot(facts=[*memory_from_benchmark_profile().facts,
                               _fact("identity.display_name", "Alex Beispiel", FactCategory.IDENTITY),
                               _fact("identity.first_name", "Alex", FactCategory.IDENTITY),
                               _fact("identity.last_name", "Beispiel", FactCategory.IDENTITY),
                               _fact("contact.email", "alex.beispiel@example.org", FactCategory.CONTACT),
                               _fact("contact.city", "Musterstadt, Allemagne", FactCategory.CONTACT),
                               _fact("identity.form_gender", "masculin / Male", FactCategory.IDENTITY),
                               _fact("education.institution", "Hochschule Musterstadt", FactCategory.EDUCATION),
                               _fact("education.bachelor_expected_end", "2027-09-30", FactCategory.EDUCATION)])
LIBRARY = build_library(HERE / "fixtures" / "memory_root")


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        instance = p.chromium.launch()
        yield instance
        instance.close()


@pytest.fixture
def page(browser):
    context = browser.new_context()
    page = context.new_page()
    page.goto(PAGE)
    yield page
    context.close()


def prepared(tmp_path: Path) -> Path:
    text = json.loads((BENCH / "cases" / "A01.json").read_text(encoding="utf-8"))["input"]
    extraction = OfferExtraction.model_validate(json.loads((BENCH / "expected_reviewed" / "A01.json").read_text(encoding="utf-8")))
    verified = verify_extraction(extraction, text)
    evaluation = evaluate_offer(verified, text, MEMORY, as_of=TODAY)
    dossier = create_dossier(offer_text=text, verified=verified, evaluation=evaluation, memory=MEMORY, root=tmp_path, as_of=TODAY)
    render_documents(dossier.folder, memory=MEMORY, library=LIBRARY, as_of=TODAY)
    return dossier.folder


def test_cookie_banner_gets_the_privacy_preserving_choice(page) -> None:
    portal = WebPortal(page, name="Personio (TEST)")
    assert portal.dismiss_cookies() == "Only necessary"
    assert page.evaluate("window.cookieChoice") == "necessary"


def test_observe_reads_labels_kinds_and_options(page) -> None:
    fields = {f.label: f for f in WebPortal(page, name="t").observe().fields}
    assert fields["First name"].required and fields["Gender"].options == ["Male", "Female", "Diverse", "Undefined"]
    java = fields["Do you have previous experience using JAVA programming language?"]
    assert java.kind.value == "radio" and java.options == ["Yes", "No"]
    assert fields["CV"].kind.value == "file" and fields["Available from"].kind.value == "date"
    assert any(f.kind.value == "checkbox" and "Frankfurt" in f.label for f in fields.values())


def test_full_real_browser_flow_submits_once(page, tmp_path) -> None:
    folder = prepared(tmp_path)
    portal = WebPortal(page, name="Personio (TEST)", dry_run=False)
    portal.dismiss_cookies()
    first = fill_application(folder, portal, memory=MEMORY, as_of=TODAY)
    assert first.state == S.AWAITING_USER
    asked = " | ".join(first.questions)
    assert "How did you find" in asked and "Visa" in asked and "Frankfurt" in asked
    assert page.input_value("#first") == "Alex" and page.input_value("#available") == "2026-10-15"
    assert page.input_value("#why").startswith("Mit großem Interesse")
    questions = {q["label"]: q["field_id"] for q in json.loads((folder / "form_plan.json").read_text(encoding="utf-8"))["answers"] if q["action"] == "ASK"}
    geo = next(v for k, v in questions.items() if "Frankfurt" in k)
    save_user_answers(folder, {questions["How did you find this position?"]: "Direct Search",
                               questions["Visa requirements"]: "I have a valid visa", geo: "checked"})
    apply_user_action(folder, "resume")
    assert fill_application(folder, portal, memory=MEMORY, as_of=TODAY).state == S.READY_TO_SUBMIT
    assert page.is_checked("#geo") and page.eval_on_selector("#cv", "el => el.files[0].name").endswith(".pdf")
    authorize_submission(folder)
    result = submit_application(folder, portal)
    assert result.state == S.CONFIRMED and "THANK YOU" in result.detail["message"]
    assert page.evaluate("window.submissions") == 1


def test_dry_run_never_clicks(page, tmp_path) -> None:
    with pytest.raises(PortalError):
        WebPortal(page, name="t").submit()
    assert page.evaluate("window.submissions || 0") == 0


def test_captcha_stops_the_robot(page) -> None:
    page.set_content("<form><div class='g-recaptcha'></div><label for='a'>Name</label><input id='a'></form>")
    with pytest.raises(PortalError):
        WebPortal(page, name="t").observe()


def test_personio_url_normalisation() -> None:
    assert personio_apply_url("https://360t.jobs.personio.de/job/363251") == "https://360t.jobs.personio.de/job/363251/apply"
    assert personio_apply_url("https://acme.jobs.personio.com/job/1?language=de") == "https://acme.jobs.personio.com/job/1/apply?language=de"
    with pytest.raises(ValueError):
        personio_apply_url("https://evil.example/job/1")


def test_submission_in_a_fresh_browser_restores_exactly_the_validated_form(browser, tmp_path) -> None:
    from bewerbungspilot.browser.runner import SubmissionRefused, restore_form
    folder = prepared(tmp_path)
    first = browser.new_page()
    first.goto(PAGE)
    portal = WebPortal(first, name="Personio (TEST)")
    fill_application(folder, portal, memory=MEMORY, as_of=TODAY)
    questions = {q["label"]: q["field_id"] for q in json.loads((folder / "form_plan.json").read_text(encoding="utf-8"))["answers"] if q["action"] == "ASK"}
    geo = next(v for k, v in questions.items() if "Frankfurt" in k)
    save_user_answers(folder, {questions["How did you find this position?"]: "Direct Search",
                               questions["Visa requirements"]: "I have a valid visa", geo: "checked"})
    apply_user_action(folder, "resume")
    assert fill_application(folder, portal, memory=MEMORY, as_of=TODAY).state == S.READY_TO_SUBMIT
    first.close()

    changed = MemorySnapshot(facts=[f if f.key != "contact.city" else f.model_copy(update={"value": "Anderstadt"}) for f in MEMORY.facts])
    fresh = browser.new_page()
    fresh.goto(PAGE)
    with pytest.raises(SubmissionRefused):
        restore_form(folder, WebPortal(fresh, name="t"), memory=changed, as_of=TODAY)

    fresh.goto(PAGE)
    portal = WebPortal(fresh, name="Personio (TEST)", dry_run=False)
    restore_form(folder, portal, memory=MEMORY, as_of=TODAY)
    authorize_submission(folder)
    assert submit_application(folder, portal).state == S.CONFIRMED
    assert fresh.evaluate("window.submissions") == 1


PERSONIO_2026 = (HERE / "fixtures" / "portals" / "personio_2026_de.html").as_uri()
FULL_MEMORY = MemorySnapshot(facts=[*MEMORY.facts,
                                    _fact("identity.github", "https://github.com/alex-beispiel", FactCategory.IDENTITY),
                                    _fact("contact.phone_international", "+49 151 0000000", FactCategory.CONTACT),
                                    _fact("language.allemand", "très bon niveau", FactCategory.LANGUAGE),
                                    _fact("language.anglais", "bon niveau", FactCategory.LANGUAGE)])


def test_live_personio_2026_markup(browser, tmp_path) -> None:
    """27/09/2026 live run: '*\\n(erforderlich)' was not read as required, the hidden file inputs were
    ignored, and the dossier reached READY_TO_SUBMIT with three required fields empty and no CV."""
    context = browser.new_context()
    page = context.new_page()
    page.goto(PERSONIO_2026)
    fields = {f.id: f for f in WebPortal(page, name="t").observe().fields}
    assert fields["id-field-email"].required and fields["id-field-email"].label == "E-Mail"
    assert fields["id-field-custom_attribute_2"].required
    assert fields["id-doc-input-cv"].kind == "file" and fields["id-doc-input-cv"].required
    assert fields["id-doc-input-cv"].label == "Lebenslauf"
    assert not fields["id-doc-input-cover-letter"].required and fields["id-doc-input-cover-letter"].label == "Anschreiben"

    # Without GitHub / phone facts: required unknowns stop the robot.
    folder = prepared(tmp_path / "a")
    first = fill_application(folder, WebPortal(page, name="t"), memory=MEMORY, as_of=TODAY)
    assert first.state == S.AWAITING_USER
    asked = " | ".join(first.questions)
    assert "GitHub" in asked and "Telefon" in asked

    # With them: everything filled, CV + letter uploaded (read back from the page's own list).
    page.goto(PERSONIO_2026)
    folder = prepared(tmp_path / "b")
    result = fill_application(folder, WebPortal(page, name="t"), memory=FULL_MEMORY, as_of=TODAY)
    assert result.state == S.READY_TO_SUBMIT, result.questions
    assert page.eval_on_selector("#field-custom_attribute_2", "el => el.options[el.selectedIndex].text") == "Fließend"
    assert page.eval_on_selector("#field-custom_attribute_3", "el => el.options[el.selectedIndex].text") == "Fortgeschritten"
    listed = page.inner_text("form")
    assert "Lebenslauf" in listed and ".pdf" in listed
    plan = {a["field_id"]: a for a in json.loads((folder / "form_plan.json").read_text(encoding="utf-8"))["answers"]}
    assert plan["id-doc-input-cv"]["action"] == "UPLOAD" and plan["id-doc-input-cover-letter"]["action"] == "UPLOAD"
    assert plan["id-doc-input-employment-reference"]["action"] == "SKIP"
    assert page.evaluate("window.submissions || 0") == 0
    context.close()
