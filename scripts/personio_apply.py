"""Personio: prepare and fill a real application in a visible browser — stops before sending.

Usage (from ``app``, Ollama running, Playwright installed with scripts\\windows\\install_browser.bat):
    .\\.venv\\Scripts\\python.exe scripts\\personio_apply.py --url https://<firma>.jobs.personio.de/job/<id>
        → reads the offer, evaluates it, prepares dossier + PDFs, fills the form, STOPS before "Submit".
    .\\.venv\\Scripts\\python.exe scripts\\personio_apply.py --url ... (again, after answering questions in the dashboard)
        → fills again with your answers; if everything is verified the dossier becomes READY_TO_SUBMIT.
    .\\.venv\\Scripts\\python.exe scripts\\personio_apply.py --url ... --submit
        → only after "Autoriser l'envoi" in the dashboard: refills, checks it is identical, clicks ONCE.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bewerbungspilot.api.dossiers import apply_user_action  # noqa: E402
from bewerbungspilot.browser.portal import PortalError  # noqa: E402
from bewerbungspilot.browser.runner import (  # noqa: E402
    SubmissionRefused, fill_application, reopen_form, restore_form, submit_application)
from bewerbungspilot.browser.web_portal import WebPortal, personio_apply_url  # noqa: E402
from bewerbungspilot.core.config import load_models_config  # noqa: E402
from bewerbungspilot.core.errors import BewerbungspilotError  # noqa: E402
from bewerbungspilot.core.journal import BusinessJournal, RunContext  # noqa: E402
from bewerbungspilot.documents.dossier import create_dossier  # noqa: E402
from bewerbungspilot.documents.render import render_documents  # noqa: E402
from bewerbungspilot.domain.enums import ApplicationState as S  # noqa: E402
from bewerbungspilot.jobs.evaluation import evaluate_offer  # noqa: E402
from bewerbungspilot.jobs.extraction import extract_with_model, verify_extraction  # noqa: E402
from bewerbungspilot.llm.ollama import OllamaClient  # noqa: E402
from bewerbungspilot.memory.store import MemoryStore  # noqa: E402

DOSSIERS = ROOT / "data" / "generated" / "applications"


def find_dossier(url: str) -> Path | None:
    for meta in DOSSIERS.glob("*/meta.json"):
        if json.loads(meta.read_text(encoding="utf-8")).get("url") == url:
            return meta.parent
    return None


def say(message: str) -> None:
    print(message, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Personio application (stops before sending unless --submit)")
    parser.add_argument("--url", required=True)
    parser.add_argument("--submit", action="store_true")
    parser.add_argument("--no-wait", action="store_true", help="lancé depuis le tableau de bord : pas de saisie clavier")
    args = parser.parse_args()

    def pause(message: str) -> None:
        if args.no_wait:
            say(message.replace("Appuie sur Entrée pour fermer", "Fermeture automatique dans 2 minutes (ou « Arrêter la tâche »)"))
            try:
                page.wait_for_timeout(120_000)
            except Exception:  # noqa: BLE001 - browser closed by the user
                pass
        else:
            input(message)
    job_url = args.url.split("/apply")[0].split("?")[0]
    apply_url = personio_apply_url(args.url)

    from playwright.sync_api import sync_playwright

    store = MemoryStore(ROOT / "data" / "private")
    memory, library = store.load(), store.load_cv_library()
    index = store.root / "applications_index.json"
    past = json.loads(index.read_text(encoding="utf-8"))["applications"] if index.exists() else []
    today = date.today()
    run = RunContext(purpose=f"personio {job_url}")

    with sync_playwright() as p, BusinessJournal(ROOT / "logs") as journal:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        folder = find_dossier(job_url)
        if folder is None:
            say("1/4 Lecture de l'annonce…")
            page.goto(job_url)
            WebPortal(page, name="Personio").dismiss_cookies()
            offer_text = page.inner_text("main") if page.locator("main").count() else page.inner_text("body")
            say("2/4 Extraction par le modèle local et évaluation par le code…")
            client = OllamaClient(load_models_config(ROOT / "config" / "models.example.yaml").models["strict_text"])
            try:
                extraction = extract_with_model(client, offer_text, run_id=str(run.run_id))
            finally:
                try:
                    client.unload()
                except BewerbungspilotError:
                    pass
            verified = verify_extraction(extraction, offer_text)
            evaluation = evaluate_offer(verified, offer_text, memory, as_of=today)
            a = evaluation.assessment
            say(f"   Décision : {a.decision.value} ({a.score}/100)")
            for label, items in (("Blocage", a.blockers), ("À vérifier", a.unknowns), ("Lacune", a.gaps), ("Point fort", a.matches)):
                for item in items[:6]:
                    say(f"     {label} : {item.statement}")
            for note in evaluation.notes:
                say(f"     Note : {note}")
            if verified.removed:
                say(f"     Retiré de l'extraction (sans preuve) : {len(verified.removed)} élément(s)")
            journal.record_offer_decision(run, a.job_offer_id, a.decision.value,
                                          f"{job_url}: " + "; ".join(i.statement for i in (a.blockers or a.unknowns or a.matches)[:3]))
            if a.decision.value == "REJECT":
                rejected = ROOT / "data" / "generated" / "rejected"
                rejected.mkdir(parents=True, exist_ok=True)
                (rejected / f"{today:%Y-%m-%d}_{job_url.split('//')[1].split('.')[0]}_{job_url.rstrip('/').split('/')[-1]}.json").write_text(
                    json.dumps({"url": job_url, "offer_text": offer_text, "extraction": verified.extraction.model_dump(),
                                "removed": verified.removed, "evaluation": evaluation.model_dump(mode="json")},
                               ensure_ascii=False, indent=2), encoding="utf-8")
                say("   Détails enregistrés dans data\\generated\\rejected")
            try:
                dossier = create_dossier(offer_text=offer_text, verified=verified, evaluation=evaluation, memory=memory,
                                         root=DOSSIERS, as_of=today, past_applications=past, run_id=run.run_id, url=job_url)
            except BewerbungspilotError as exc:
                say(f"   Pas de dossier : {exc}")
                return 2
            folder = dossier.folder
            for record in dossier.transitions:
                journal.record_transition(run, record)
            if dossier.application.state == S.SELECTED:
                say("3/4 CV et lettre en PDF…")
                rendered = render_documents(folder, memory=memory, library=library, as_of=today,
                                            past_companies=[a.get("company") for a in past], run_id=run.run_id)
                if rendered.transition:
                    journal.record_transition(run, rendered.transition)
        application = json.loads((folder / "application.json").read_text(encoding="utf-8"))
        state = application["state"]
        say(f"   Dossier : {folder.name} — état {state}")
        if state == S.AWAITING_USER and application.get("resume_state") == S.EVALUATED:
            say("   Offre « à vérifier » : lis les points ci-dessus dans le tableau de bord, puis clique")
            say("   « Candidater quand même » si tu veux postuler, et relance cette commande.")
            pause("Appuie sur Entrée pour fermer le navigateur…")
            browser.close()
            return 0
        if state == S.SELECTED:
            say("3/4 CV et lettre en PDF…")
            render_documents(folder, memory=memory, library=library, as_of=today, past_companies=[a.get("company") for a in past])
            state = json.loads((folder / "application.json").read_text(encoding="utf-8"))["state"]
        if state not in (S.DOCUMENTS_PREPARED, S.FORM_IN_PROGRESS, S.READY_TO_SUBMIT, S.AWAITING_USER):
            say("   Rien à remplir dans cet état.")
            return 2
        if state == S.AWAITING_USER and application.get("resume_state") == S.FORM_IN_PROGRESS:
            if not (folder / "user_answers.json").exists():
                say("   Des questions attendent tes réponses dans le tableau de bord.")
                return 2
            apply_user_action(folder, "resume")

        page.goto(apply_url)
        portal = WebPortal(page, name="Personio", dry_run=not args.submit)
        portal.dismiss_cookies()
        shots = folder / "_previews"
        shots.mkdir(exist_ok=True)
        try:
            state = json.loads((folder / "application.json").read_text(encoding="utf-8"))["state"]
            if args.submit:
                say("4/4 Envoi : remplissage identique à ce que tu as validé…")
                restore_form(folder, portal, memory=memory, as_of=today)
                portal.screenshot(shots / f"form_before_submit_{datetime.now():%Y%m%d_%H%M%S}.png")
                result = submit_application(folder, portal, run_id=run.run_id)
                for record in result.transitions:
                    journal.record_transition(run, record)
                portal.screenshot(shots / f"form_after_submit_{datetime.now():%Y%m%d_%H%M%S}.png")
                say(f"   Résultat : {result.state} — {result.detail}")
            elif state == S.READY_TO_SUBMIT:
                try:
                    restore_form(folder, portal, memory=memory, as_of=today)
                    portal.screenshot(shots / f"form_ready_{datetime.now():%Y%m%d_%H%M%S}.png")
                    say("   Formulaire prêt et vérifié. Pour envoyer : « Autoriser l'envoi » dans le tableau de bord, puis --submit.")
                    state = None
                except SubmissionRefused as exc:
                    say(f"   Le formulaire ne correspond plus à ce qui était prêt ({exc}) : on le refait.")
                    reopened = reopen_form(folder, str(exc), run_id=run.run_id)
                    for record in reopened.transitions:
                        journal.record_transition(run, record)
                    page.goto(apply_url)
                    portal = WebPortal(page, name="Personio", dry_run=True)
                    portal.dismiss_cookies()
                    state = S.FORM_IN_PROGRESS
            if state is not None and state != S.READY_TO_SUBMIT and not args.submit:
                say("4/4 Remplissage du formulaire (aucun envoi)…")
                result = fill_application(folder, portal, memory=memory, as_of=today, run_id=run.run_id)
                for record in result.transitions:
                    journal.record_transition(run, record)
                portal.screenshot(shots / f"form_filled_{datetime.now():%Y%m%d_%H%M%S}.png")
                say(f"   État : {result.state}")
                for question in result.questions:
                    say(f"   ? {question}")
                if result.questions:
                    say("   → Réponds dans le tableau de bord, puis relance la même commande.")
        except (PortalError, SubmissionRefused) as exc:
            say(f"   Arrêt : {exc}")
        say("\nLe navigateur reste ouvert pour que tu vérifies. Aucun envoi n'a été fait sans --submit.")
        pause("Appuie sur Entrée pour fermer le navigateur…")
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
