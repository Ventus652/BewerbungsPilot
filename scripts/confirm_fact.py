"""Record an explicit user confirmation or correction in the private memory.

Usage (from ``app``):
    .\\.venv\\Scripts\\python.exe scripts\\confirm_fact.py identity.legal_name "Prénom Nom"

The value becomes a USER_CORRECTION (highest authority). An existing fact keeps its
category and policy; ``needs_review`` is cleared. Any difference stays visible as a conflict.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bewerbungspilot.core.journal import BusinessJournal, RunContext  # noqa: E402
from bewerbungspilot.domain.enums import DisclosurePolicy, FactCategory, FactStatus, SourceRank  # noqa: E402
from bewerbungspilot.domain.models import CandidateFact  # noqa: E402
from bewerbungspilot.memory.store import MemoryStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Confirm or correct one fact")
    parser.add_argument("key")
    parser.add_argument("value", help="texte, ou JSON avec --json")
    parser.add_argument("--json", action="store_true", help="la valeur est du JSON")
    parser.add_argument("--create", action="store_true", help="créer un fait déclaré par l'utilisateur")
    parser.add_argument("--category", default=None, help="catégorie (avec --create), ex. SKILL, IDENTITY")
    parser.add_argument("--policy", default="APPLICATION_STANDARD", help="politique de divulgation (avec --create)")
    args = parser.parse_args()
    value = json.loads(args.value) if args.json else args.value

    store = MemoryStore(ROOT / "data" / "private")
    memory = store.load()
    current = memory.fact(args.key)
    if current is None and not args.create:
        print(f"Clé inconnue : {args.key}. Aucune création implicite (utiliser --create --category ...).")
        return 1
    if current is None:
        if not args.category:
            print("--category est obligatoire avec --create")
            return 1
        current = CandidateFact(key=args.key, value=value, category=FactCategory(args.category), status=FactStatus.CONFIRMED,
                                source="création utilisateur", validated_at=datetime.now(timezone.utc),
                                disclosure_policy=DisclosurePolicy(args.policy))
    run = RunContext(purpose=f"user confirmation {args.key}")
    confirmed = CandidateFact.model_validate(
        {
            **current.model_dump(exclude={"id"}),
            "value": value,
            "status": FactStatus.SENSITIVE if current.status == FactStatus.SENSITIVE else FactStatus.CONFIRMED,
            "source": f"Confirmation explicite de l'utilisateur ({date.today().isoformat()})",
            "source_rank": SourceRank.USER_CORRECTION,
            "source_ref": "user:chat",
            "validated_at": datetime.now(timezone.utc),
            "needs_review": False,
        }
    )
    snapshot, migration = store.ingest(memory, [confirmed], [], run_id=run.run_id, kind="user_confirmation",
                                       notes=[f"confirm {args.key}"])
    store.save(snapshot)
    with BusinessJournal(ROOT / "logs") as journal:
        journal.write(run, "user_confirmation", key=args.key, updated=migration.facts_updated,
                      conflicts=migration.conflicts_resolved_by_priority)
    fact = snapshot.fact(args.key)
    print(f"{args.key} : source={fact.source_rank}, à vérifier={fact.needs_review}, "
          f"conflit enregistré={bool(migration.conflicts_resolved_by_priority)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
