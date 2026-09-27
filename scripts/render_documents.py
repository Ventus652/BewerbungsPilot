"""(Re)render and verify the PDFs of one dossier — e.g. after editing Motivationsschreiben.md.

Usage (from ``app``):
    .\\.venv\\Scripts\\python.exe scripts\\render_documents.py data\\generated\\applications\\<dossier>
    ... --with-address   include the full postal address in the letter header (explicit choice)
The edited letter is checked again; a refused letter is rendered but never marked valid.
Exit code: 0 all documents valid, 1 missing input, 3 a check failed.
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

from bewerbungspilot.core.journal import BusinessJournal, RunContext  # noqa: E402
from bewerbungspilot.documents.render import render_documents  # noqa: E402
from bewerbungspilot.memory.store import MemoryStore  # noqa: E402


def run(folder: Path, *, with_address: bool = False) -> int:
    store = MemoryStore(ROOT / "data" / "private")
    memory, library = store.load(), store.load_cv_library()
    if not memory.facts or not library:
        print("Mémoire ou bibliothèque de CV vide : lancer scripts/import_memory.py")
        return 1
    index = store.root / "applications_index.json"
    past = [a.get("company") for a in json.loads(index.read_text(encoding="utf-8"))["applications"]] if index.exists() else []
    run_ctx = RunContext(purpose=f"render documents {folder.name}")
    result = render_documents(folder, memory=memory, library=library, as_of=date.today(), past_companies=past,
                              include_address=with_address, run_id=run_ctx.run_id)
    with BusinessJournal(ROOT / "logs") as journal:
        for document in result.documents:
            journal.write(run_ctx, "document_rendered", application_id=document.application_id, document_type=document.document_type,
                          sha256=document.sha256, visual_validation=document.visual_validation)
        if result.transition:
            journal.record_transition(run_ctx, result.transition)
    ok = result.cv.ok and result.letter.ok
    print(json.dumps({
        "status": "DOCUMENTS_PREPARED" if result.transition else ("VALID" if ok else "CHECK_FAILED"),
        "state": result.application.state.value,
        "base_cv": result.base_cv,
        "cv": {"file": Path(result.cv.path).name, "ok": result.cv.ok, "issues": result.cv.issues},
        "letter": {"file": Path(result.letter.path).name, "ok": result.letter.ok, "issues": result.letter.issues},
        "excluded_cvs": result.excluded_cvs,
    }, ensure_ascii=False, indent=2))
    return 0 if ok else 3


def main() -> int:
    parser = argparse.ArgumentParser(description="Render and verify dossier PDFs")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--with-address", action="store_true")
    args = parser.parse_args()
    folder = args.folder if args.folder.is_absolute() else ROOT / args.folder
    if not (folder / "meta.json").exists():
        print(f"Dossier introuvable : {folder}")
        return 1
    return run(folder, with_address=args.with_address)


if __name__ == "__main__":
    raise SystemExit(main())
