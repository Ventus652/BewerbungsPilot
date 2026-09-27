"""Demonstration of the complete flow on a FICTIONAL portal with the anonymous test profile.

Usage (from ``app``):  .\\.venv\\Scripts\\python.exe scripts\\demo_form.py
Writes only into data/generated/demo (never into your real dossiers, never a real receipt).
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bewerbungspilot.api.dossiers import apply_user_action  # noqa: E402
from bewerbungspilot.browser.portal import FakePortal  # noqa: E402
from bewerbungspilot.browser.runner import authorize_submission, fill_application, recover_submission, submit_application  # noqa: E402
from bewerbungspilot.documents.dossier import create_dossier  # noqa: E402
from bewerbungspilot.documents.render import render_documents  # noqa: E402
from bewerbungspilot.domain.enums import FactCategory  # noqa: E402
from bewerbungspilot.jobs.evaluation import evaluate_offer  # noqa: E402
from bewerbungspilot.jobs.extraction import OfferExtraction, verify_extraction  # noqa: E402
from bewerbungspilot.memory.benchmark import _fact, memory_from_benchmark_profile  # noqa: E402
from bewerbungspilot.memory.cv_library import build_library  # noqa: E402
from bewerbungspilot.memory.records import MemorySnapshot  # noqa: E402

DEMO = ROOT / "data" / "generated" / "demo"


def main() -> int:
    if DEMO.exists():
        shutil.rmtree(DEMO)
    today = date.today()
    memory = MemorySnapshot(facts=[*memory_from_benchmark_profile().facts,
                                   _fact("identity.display_name", "Alex Beispiel", FactCategory.IDENTITY),
                                   _fact("contact.email", "alex.beispiel@example.org", FactCategory.CONTACT),
                                   _fact("education.institution", "Hochschule Musterstadt", FactCategory.EDUCATION),
                                   _fact("education.bachelor_expected_end", "2027-09-30", FactCategory.EDUCATION)])
    library = build_library(ROOT / "tests" / "fixtures" / "memory_root")
    text = json.loads((ROOT / "benchmarks" / "cases" / "A01.json").read_text(encoding="utf-8"))["input"]
    extraction = OfferExtraction.model_validate(json.loads((ROOT / "benchmarks" / "expected_reviewed" / "A01.json").read_text(encoding="utf-8")))
    verified = verify_extraction(extraction, text)
    evaluation = evaluate_offer(verified, text, memory, as_of=today)
    steps = []
    dossier = create_dossier(offer_text=text, verified=verified, evaluation=evaluation, memory=memory, root=DEMO, as_of=today)
    steps.append(f"dossier : {dossier.application.state}")
    steps.append(f"PDF : {render_documents(dossier.folder, memory=memory, library=library, as_of=today).application.state}")
    portal = FakePortal(crash_on_submit=True)
    first = fill_application(dossier.folder, portal, memory=memory, as_of=today)
    steps.append(f"1er remplissage : {first.state} — questions : {len(first.questions)}")
    for question in first.questions:
        steps.append(f"   ? {question}")
    (dossier.folder / "user_answers.json").write_text(json.dumps(
        {"vorname": "Alex", "nachname": "Beispiel", "arbeitserlaubnis": "Ja", "datenschutz": "akzeptiert"}), encoding="utf-8")
    steps.append("réponses de l'utilisateur (simulées pour la démo) enregistrées")
    steps.append(f"reprise : {apply_user_action(dossier.folder, 'resume')['state']}")
    steps.append(f"2e remplissage : {fill_application(dossier.folder, portal, memory=memory, as_of=today).state}")
    authorize_submission(dossier.folder, by="démo")
    steps.append("autorisation d'envoi donnée (une seule fois)")
    steps.append(f"envoi (le portail coupe après le clic) : {submit_application(dossier.folder, portal).state}")
    steps.append(f"vérification sans recliquer : {recover_submission(dossier.folder, portal).state}")
    steps.append(f"nombre de clics sur « Absenden » : {portal.submissions}")
    print("\n".join(steps))
    print(f"\nDossier de démonstration : {dossier.folder.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
