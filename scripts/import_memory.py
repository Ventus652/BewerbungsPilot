"""Import the reference profile into the private memory (phase 3.3).

Usage (from ``app``):
    .\\.venv\\Scripts\\python.exe scripts\\import_memory.py            # import
    .\\.venv\\Scripts\\python.exe scripts\\import_memory.py --dry-run  # show what would change
    .\\.venv\\Scripts\\python.exe scripts\\import_memory.py --show-conflicts

Reads ``profile_sources`` by default and never modifies it. Writes only to
``data/private`` (ignored by Git). The journals receive counts only, never values.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bewerbungspilot.core.journal import BusinessJournal, RunContext, TechnicalJournal  # noqa: E402
from bewerbungspilot.memory.cv_library import build_library  # noqa: E402
from bewerbungspilot.memory.importer import import_sources  # noqa: E402
from bewerbungspilot.memory.records import ConflictStatus  # noqa: E402
from bewerbungspilot.memory.store import MemoryStore  # noqa: E402

DEFAULT_SOURCE = ROOT / "profile_sources"


def main() -> int:
    parser = argparse.ArgumentParser(description="Import the reference profile into data/private")
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--private", type=Path, default=ROOT / "data" / "private")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--show-conflicts", action="store_true", help="print conflicting values locally")
    args = parser.parse_args()

    run = RunContext(purpose="memory import" + (" dry-run" if args.dry_run else ""))
    store = MemoryStore(args.private)
    with TechnicalJournal(ROOT / "logs") as tech, BusinessJournal(ROOT / "logs") as business:
        try:
            result = import_sources(args.source_root)
        except FileNotFoundError as exc:
            tech.record(run, "memory.import", "source_missing", error=str(exc))
            print(json.dumps({"status": "SOURCE_MISSING", "error": str(exc)}, ensure_ascii=False, indent=2))
            return 1
        before = store.load()
        snapshot, migration = store.ingest(
            before, result.facts, result.sources, run_id=run.run_id, kind="reference_import", notes=result.notes
        )
        cv_library = build_library(args.source_root)
        if not args.dry_run:
            store.save(snapshot)
            store.save_applications_index(result.applications)
            store.save_cv_library(cv_library)
        summary = {
            "status": "DRY_RUN" if args.dry_run else "IMPORTED",
            "run_id": str(run.run_id),
            "facts_total": len(snapshot.facts),
            "facts_by_category": dict(sorted(Counter(f.category.value for f in snapshot.facts).items())),
            "facts_added": migration.facts_added,
            "facts_updated": migration.facts_updated,
            "facts_needing_review": sorted(f.key for f in snapshot.facts if f.needs_review),
            "sources": len(snapshot.sources),
            "sources_not_processed": [s.path for s in snapshot.sources if not s.processed],
            "past_applications_indexed": len(result.applications),
            "validated_cvs_parsed": len(cv_library),
            "conflicts_open": sorted(c.key for c in snapshot.conflicts if c.status == ConflictStatus.OPEN),
            "conflicts_resolved_by_priority": sorted(
                c.key for c in snapshot.conflicts if c.status == ConflictStatus.RESOLVED_BY_PRIORITY
            ),
        }
        tech.record(run, "memory.import", "import_done", **{k: v for k, v in summary.items() if k != "facts_needing_review"})
        business.write(run, "memory_import", facts_added=migration.facts_added,
                       conflicts_open=len(summary["conflicts_open"]), dry_run=args.dry_run)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.show_conflicts:
        for conflict in snapshot.conflicts:
            print(f"\n[{conflict.status}] {conflict.key}")
            for value in conflict.values:
                print(f"  - {value.value!r}  ← {value.source} ({value.rank})")
            print(f"  action : {conflict.required_action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
