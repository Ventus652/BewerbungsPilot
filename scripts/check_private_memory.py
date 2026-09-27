"""Replay the phase 3 memory checks on the real private memory, without printing values.

Usage (from ``app``):
    .\\.venv\\Scripts\\python.exe scripts\\check_private_memory.py
Exit code 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bewerbungspilot.domain.enums import FactCategory  # noqa: E402
from bewerbungspilot.memory.context import TaskType, build_context, rank_projects  # noqa: E402
from bewerbungspilot.memory.records import ConflictStatus  # noqa: E402
from bewerbungspilot.memory.rules import PERMIT_UNTIL, ProfileRules, RuleOutcome  # noqa: E402
from bewerbungspilot.memory.store import MemoryStore, assert_no_secret  # noqa: E402


def main() -> int:
    today = date.today()
    store = MemoryStore(ROOT / "data" / "private")
    memory = store.load()
    if not memory.facts:
        print("Mémoire privée vide : lancer d'abord scripts/import_memory.py")
        return 1
    rules = ProfileRules(memory, as_of=today)
    address = memory.fact("contact.postal_address")
    address_text = str(address.value) if address else "\x00"
    results: list[tuple[str, bool]] = []

    def check(name: str, ok: bool) -> None:
        results.append((name, bool(ok)))

    check("chaque fait possède source et date de validation", all(f.source and f.validated_at for f in memory.facts))
    try:
        assert_no_secret(memory.facts)
        check("aucun identifiant de connexion stocké", True)
    except ValueError:
        check("aucun identifiant de connexion stocké", False)

    analysis = build_context(memory, TaskType.OFFER_ANALYSIS, as_of=today, query="Werkstudent Softwareentwicklung Java")
    dumped = json.dumps(analysis.model_dump(mode="json"), ensure_ascii=False)
    check("analyse d'offre sans adresse complète", address_text not in dumped)
    check("analyse d'offre sans donnée légale", not any(k.startswith("legal.") for k in analysis.keys()))

    letter = build_context(memory, TaskType.COVER_LETTER, as_of=today, query="Java", requested_keys=[PERMIT_UNTIL])
    check("lettre sans titre de séjour", not any(k.startswith("legal.") for k in letter.keys()))

    for label, query, tag in (
        ("lettre Java → projet Java en premier", "Werkstudent Java Backend", "java"),
        ("requête frontend → projet React en premier", "Frontend React TypeScript", "react"),
        ("requête data → projet Python en premier", "Data Science Python Machine Learning", "python"),
    ):
        ranked = rank_projects((f for f in memory.facts if f.category == FactCategory.PROJECT), query)
        check(label, bool(ranked) and tag in {t.casefold() for t in ranked[0][0].tags})

    permit = memory.fact(PERMIT_UNTIL)
    if permit is not None:
        after = date.fromisoformat(str(permit.value)) + timedelta(days=1)
        decision = rules.work_authorization(after)
        renewed = rules.fact("legal.residence_permit_renewal_confirmed")
        expected = RuleOutcome.ALLOW if renewed is not None and renewed.value is True else RuleOutcome.AWAITING_USER
        check("question d'autorisation après expiration → blocage humain", decision.outcome == expected)
    check("compétence absente → inconnue", rules.claim_skill("COBOL").outcome == RuleOutcome.UNKNOWN)
    terms = rules.forbidden_claim_terms()
    check("revendications interdites bloquées", bool(terms) and all(rules.claim_skill(t).outcome == RuleOutcome.BLOCK for t in terms))
    check("aucun niveau CECR inventé", all(rules.language_claim(f.key, "C2").outcome == RuleOutcome.BLOCK
                                         for f in memory.facts if f.key.startswith("language.") and "C2" not in str(f.value)))
    check("faits expirés jamais courants", not any(f.is_current(today) for f in memory.facts if f.status.value == "EXPIRED"))

    with tempfile.TemporaryDirectory() as tmp:
        exported = store.export(Path(tmp) / "export.json")
        restored = MemoryStore(Path(tmp) / "restored").restore(exported)
        check("export et restauration identiques", restored.model_dump(mode="json")["facts"] == memory.model_dump(mode="json")["facts"])

    open_conflicts = [c.key for c in memory.conflicts if c.status == ConflictStatus.OPEN]
    for name, ok in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    print(f"\nFaits : {len(memory.facts)} ; conflits ouverts à décider : {len(open_conflicts)} {open_conflicts}")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
