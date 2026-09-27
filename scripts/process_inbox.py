"""Process the offers dropped in data/inbox (one .md or .txt file per offer). Nothing is sent.

Usage (from ``app``, Ollama running):  .\\.venv\\Scripts\\python.exe scripts\\process_inbox.py
Pause / Resume / Stop from the dashboard are honoured between two offers.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bewerbungspilot.api.control import RunControl, RunState  # noqa: E402
from bewerbungspilot.api.pipeline import run_inbox  # noqa: E402
from bewerbungspilot.core.config import load_models_config  # noqa: E402
from bewerbungspilot.core.errors import BewerbungspilotError  # noqa: E402
from bewerbungspilot.jobs.extraction import extract_with_model  # noqa: E402
from bewerbungspilot.llm.ollama import OllamaClient  # noqa: E402
from bewerbungspilot.memory.store import MemoryStore  # noqa: E402


def main() -> int:
    inbox = ROOT / "data" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    store = MemoryStore(ROOT / "data" / "private")
    memory, library = store.load(), store.load_cv_library()
    if not memory.facts:
        print("Mémoire vide : lancer scripts/import_memory.py")
        return 1
    index = store.root / "applications_index.json"
    past = json.loads(index.read_text(encoding="utf-8"))["applications"] if index.exists() else []
    control = RunControl(ROOT / "data" / "generated" / "control.json")
    if control.get() == RunState.STOPPED:
        control.set(RunState.RUNNING, by="new run")  # a new run starts fresh; Stop applies to the run in progress
    client = OllamaClient(load_models_config(ROOT / "config" / "models.example.yaml").models["strict_text"])

    def extractor(text: str, case_id: str):
        return extract_with_model(client, text, run_id=None)

    try:
        results = run_inbox(inbox=inbox, dossiers=ROOT / "data" / "generated" / "applications", logs=ROOT / "logs",
                            memory=memory, library=library, extractor=extractor, control=control,
                            past_applications=past, as_of=date.today())
    except BewerbungspilotError as exc:
        print(json.dumps({"status": "FAILED", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 3
    finally:
        try:
            client.unload()
        except BewerbungspilotError:
            pass
    print(json.dumps({"processed": results, "inbox": str(inbox)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
