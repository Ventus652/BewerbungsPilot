"""Private JSON memory store (guide 3.4).

Layout under ``data/private`` (ignored by Git)::

    profile.json      active facts
    sources.json      reference files read, with rank and SHA-256
    conflicts.json    every detected conflict, open or resolved
    policies.json     readable disclosure policy per fact key (audit view)
    migrations/       one trace file per import or correction

Writes are atomic (temporary file + replace). No secret may enter the store. A single
readable export can be written and restored.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import UUID

from bewerbungspilot.core.secrets import contains_forbidden_key, is_credential_key, redact_text
from bewerbungspilot.domain.enums import LEGACY_POLICY_MAP, FactStatus, SourceRank
from bewerbungspilot.domain.models import CandidateFact

from .priority import merge
from .records import (
    ConflictStatus,
    FactConflict,
    MemorySnapshot,
    MigrationRecord,
    SourceRecord,
)


class SecretInMemoryError(ValueError):
    """Raised when something that looks like a credential would be stored."""


def _atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False, default=str)
    fd, tmp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _read(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def assert_no_secret(facts: Iterable[CandidateFact]) -> None:
    for fact in facts:
        if is_credential_key(fact.key):
            raise SecretInMemoryError(f"Fact key {fact.key!r} looks like a credential")
        payload = fact.model_dump(mode="json")["value"]
        if contains_forbidden_key(payload):
            raise SecretInMemoryError(f"Fact {fact.key!r} contains a secret field")
        text = json.dumps(payload, ensure_ascii=False)
        if redact_text(text) != text:
            raise SecretInMemoryError(f"Fact {fact.key!r} contains a secret-looking value")


class MemoryStore:
    def __init__(self, root: str | Path = "data/private") -> None:
        self.root = Path(root)

    # ------------------------------------------------------------------ files
    @property
    def migrations_dir(self) -> Path:
        return self.root / "migrations"

    def load(self) -> MemorySnapshot:
        migrations = []
        if self.migrations_dir.exists():
            for file in sorted(self.migrations_dir.glob("*.json")):
                migrations.append(_read(file, {}))
        return MemorySnapshot.model_validate(
            {
                "facts": _read(self.root / "profile.json", {"facts": []})["facts"],
                "sources": _read(self.root / "sources.json", {"sources": []})["sources"],
                "conflicts": _read(self.root / "conflicts.json", {"conflicts": []})["conflicts"],
                "migrations": migrations,
            }
        )

    def save(self, snapshot: MemorySnapshot) -> None:
        assert_no_secret(snapshot.facts)
        data = snapshot.model_dump(mode="json")
        _atomic_write(self.root / "profile.json", {"schema_version": data["schema_version"], "facts": data["facts"]})
        _atomic_write(self.root / "sources.json", {"sources": data["sources"]})
        _atomic_write(self.root / "conflicts.json", {"conflicts": data["conflicts"]})
        _atomic_write(
            self.root / "policies.json",
            {
                "legacy_policy_map": {k.value: v.value for k, v in LEGACY_POLICY_MAP.items()},
                "facts": {
                    f.key: {"policy": f.effective_policy.value, "sensitive": f.is_sensitive}
                    for f in snapshot.facts
                },
            },
        )
        for migration in data["migrations"]:
            stamp = migration["occurred_at"].replace(":", "").replace("-", "")[:15]
            _atomic_write(self.migrations_dir / f"{stamp}_{migration['kind']}_{migration['id'][:8]}.json", migration)

    def save_applications_index(self, entries: list[dict[str, Any]]) -> Path:
        """Index of past application folders (company, position, status) for deduplication."""

        path = self.root / "applications_index.json"
        _atomic_write(path, {"applications": entries})
        return path

    def save_cv_library(self, documents: list[Any]) -> Path:
        """Validated CVs parsed from the historical generators (private: contains contact data)."""

        path = self.root / "cv_library.json"
        _atomic_write(path, {"documents": [d.model_dump(mode="json") for d in documents]})
        return path

    def load_cv_library(self) -> list[Any]:
        from .cv_library import CvDocument

        data = _read(self.root / "cv_library.json", {"documents": []})
        return [CvDocument.model_validate(d) for d in data["documents"]]

    # ------------------------------------------------------------------ export / restore
    def export(self, destination: str | Path) -> Path:
        snapshot = self.load()
        destination = Path(destination)
        _atomic_write(destination, snapshot.model_dump(mode="json"))
        return destination

    def restore(self, source: str | Path) -> MemorySnapshot:
        snapshot = MemorySnapshot.model_validate(_read(Path(source), None))
        self.save(snapshot)
        return snapshot

    # ------------------------------------------------------------------ changes
    def ingest(
        self,
        snapshot: MemorySnapshot,
        facts: Iterable[CandidateFact],
        sources: Iterable[SourceRecord],
        *,
        run_id: UUID,
        kind: str,
        notes: list[str] | None = None,
    ) -> tuple[MemorySnapshot, MigrationRecord]:
        """Merge new facts by priority; returns a new snapshot and its migration trace."""

        incoming = list(facts)
        assert_no_secret(incoming)
        by_key = {f.key: f for f in snapshot.facts}
        conflicts = list(snapshot.conflicts)
        added = updated = opened = resolved = 0
        for fact in incoming:
            current = by_key.get(fact.key)
            if current is None:
                by_key[fact.key] = fact
                added += 1
                continue
            outcome = merge(current, fact)
            if outcome.conflict is not None:
                if not self._already_recorded(conflicts, outcome.conflict):
                    conflicts.append(outcome.conflict)
                    if outcome.conflict.status == ConflictStatus.OPEN:
                        opened += 1
                    else:
                        resolved += 1
            if outcome.retained is not current:
                by_key[fact.key] = outcome.retained
                updated += 1
        source_map = {s.id: s for s in snapshot.sources}
        for source in sources:
            previous = source_map.get(source.id)
            # A manual verification (processed=True) is never undone by a re-import.
            if previous is not None and previous.processed and not source.processed and previous.sha256 == source.sha256:
                continue
            source_map[source.id] = source
        migration = MigrationRecord(
            run_id=run_id,
            kind=kind,
            source_ids=sorted(source_map),
            facts_added=added,
            facts_updated=updated,
            conflicts_opened=opened,
            conflicts_resolved_by_priority=resolved,
            notes=notes or [],
        )
        new_snapshot = MemorySnapshot(
            facts=sorted(by_key.values(), key=lambda f: f.key),
            sources=sorted(source_map.values(), key=lambda s: s.id),
            conflicts=conflicts,
            migrations=[*snapshot.migrations, migration],
        )
        return new_snapshot, migration

    @staticmethod
    def _already_recorded(conflicts: list[FactConflict], candidate: FactConflict) -> bool:
        signature = (candidate.key, json.dumps([v.model_dump(mode="json") for v in candidate.values], sort_keys=True))
        return any(
            (c.key, json.dumps([v.model_dump(mode="json") for v in c.values], sort_keys=True)) == signature
            for c in conflicts
        )

    def resolve_conflict(
        self,
        snapshot: MemorySnapshot,
        conflict_id: UUID,
        chosen_value: Any,
        *,
        run_id: UUID,
        decided_on: date,
    ) -> tuple[MemorySnapshot, MigrationRecord]:
        """Record the user's decision as the most authoritative source (USER_CORRECTION)."""

        conflict = next((c for c in snapshot.conflicts if c.id == conflict_id), None)
        if conflict is None:
            raise KeyError(f"Unknown conflict {conflict_id}")
        current = snapshot.fact(conflict.key)
        if current is None:
            raise KeyError(f"No fact for key {conflict.key}")
        now = datetime.now(timezone.utc)
        corrected = current.model_copy(
            update={
                "value": chosen_value,
                "status": FactStatus.SENSITIVE if current.status == FactStatus.SENSITIVE else FactStatus.CONFIRMED,
                "source": f"Correction explicite de l'utilisateur ({decided_on.isoformat()})",
                "source_rank": SourceRank.USER_CORRECTION,
                "source_ref": f"conflict:{conflict.id}",
                "validated_at": now,
                "needs_review": False,
            }
        )
        corrected = CandidateFact.model_validate(corrected.model_dump())
        assert_no_secret([corrected])
        conflicts = [
            c.model_copy(
                update={
                    "status": ConflictStatus.RESOLVED_BY_USER,
                    "retained_value": corrected.model_dump(mode="json")["value"],
                    "resolved_at": now,
                    "required_action": "Résolu par l'utilisateur.",
                }
            )
            if c.id == conflict_id
            else c
            for c in snapshot.conflicts
        ]
        facts = [corrected if f.key == conflict.key else f for f in snapshot.facts]
        migration = MigrationRecord(run_id=run_id, kind="user_correction", facts_updated=1, notes=[f"conflict {conflict.key}"])
        return (
            MemorySnapshot(
                facts=facts,
                sources=snapshot.sources,
                conflicts=conflicts,
                migrations=[*snapshot.migrations, migration],
            ),
            migration,
        )
