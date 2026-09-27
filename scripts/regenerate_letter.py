"""Rebuild Motivationsschreiben.md (+ letter_check.json) of an existing dossier from its saved
extraction/evaluation and the current private memory — e.g. after fixing the letter builder or
confirming a new fact. Never touches the application state; PDFs must be re-rendered afterwards
(scripts/render_documents.py).

    python scripts/regenerate_letter.py <dossier folder name>
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bewerbungspilot.documents.checks import check_letter  # noqa: E402
from bewerbungspilot.documents.letters import build_letter  # noqa: E402
from bewerbungspilot.jobs.evaluation import OfferEvaluation  # noqa: E402
from bewerbungspilot.jobs.extraction import OfferExtraction  # noqa: E402
from bewerbungspilot.memory.rules import ProfileRules  # noqa: E402
from bewerbungspilot.memory.store import MemoryStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dossier")
    parser.add_argument("--root", type=Path, default=ROOT / "data" / "generated" / "applications")
    parser.add_argument("--salary", action="store_true")
    args = parser.parse_args()
    folder = args.root / args.dossier
    store = MemoryStore(ROOT / "data" / "private")
    memory = store.load()
    index_path = store.root / "applications_index.json"
    past = json.loads(index_path.read_text(encoding="utf-8"))["applications"] if index_path.exists() else []
    extraction = OfferExtraction.model_validate(json.loads((folder / "extraction.json").read_text(encoding="utf-8"))["extraction"])
    evaluation = OfferEvaluation.model_validate_json((folder / "evaluation.json").read_text(encoding="utf-8"))
    offer_md = (folder / "Stellenanzeige.md").read_text(encoding="utf-8")
    offer_text = offer_md.split("\n\n", 1)[1] if offer_md.startswith("# ") else offer_md
    today = date.today()
    letter = build_letter(memory, evaluation, extraction, offer_text, as_of=today, include_salary=args.salary)
    rules = ProfileRules(memory, as_of=today)
    name = rules.fact("identity.display_name")
    check = check_letter(letter.text, memory=memory, rules=rules, offer_text=offer_text, company=extraction.company or "",
                         allowed_numbers_text=letter.allowed_numbers_text,
                         other_companies=[e.get("company") for e in past if e.get("company")],
                         signature=str(name.value) if name else None)
    (folder / "Motivationsschreiben.md").write_text(letter.text, encoding="utf-8")
    (folder / "letter_check.json").write_text(json.dumps(check.model_dump(), ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(letter.text)
    print("\nContrôle :", "OK" if check.ok else "REFUSÉ — " + "; ".join(f"{i.rule}: {i.detail}" for i in check.issues))
    return 0 if check.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
