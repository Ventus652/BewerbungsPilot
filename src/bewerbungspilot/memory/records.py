"""Records kept by the local memory besides the facts themselves (phase 3.3–3.5)."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, JsonValue, field_validator

from bewerbungspilot.domain.enums import SourceRank
from bewerbungspilot.domain.models import SHA256_PATTERN, CandidateFact, DomainModel

MEMORY_SCHEMA_VERSION = 1


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SourceRecord(DomainModel):
    """A reference file read during an import; the file itself is never modified."""

    id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9_.:-]+$")
    rank: SourceRank
    path: str = Field(min_length=1, max_length=1000)
    description: str = Field(min_length=1, max_length=500)
    sha256: str | None = None
    captured_at: AwareDatetime = Field(default_factory=utc_now)
    processed: bool = True
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("sha256")
    @classmethod
    def valid_sha256(cls, value: str | None) -> str | None:
        if value is not None and not SHA256_PATTERN.fullmatch(value):
            raise ValueError("sha256 must be a lowercase SHA-256")
        return value


class ConflictStatus(StrEnum):
    OPEN = "OPEN"  # same authority, different values: the user must decide
    RESOLVED_BY_PRIORITY = "RESOLVED_BY_PRIORITY"  # objective priority applied, still visible
    RESOLVED_BY_USER = "RESOLVED_BY_USER"


class ConflictValue(DomainModel):
    value: JsonValue
    source: str
    source_ref: str | None = None
    rank: SourceRank | None = None


class FactConflict(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    key: str
    values: list[ConflictValue] = Field(min_length=2)
    status: ConflictStatus
    retained_value: JsonValue | None = None
    required_action: str = Field(min_length=1, max_length=1000)
    detected_at: AwareDatetime = Field(default_factory=utc_now)
    resolved_at: AwareDatetime | None = None


class MigrationRecord(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    kind: str = Field(min_length=1, max_length=120)
    occurred_at: AwareDatetime = Field(default_factory=utc_now)
    source_ids: list[str] = Field(default_factory=list)
    facts_added: int = 0
    facts_updated: int = 0
    conflicts_opened: int = 0
    conflicts_resolved_by_priority: int = 0
    notes: list[str] = Field(default_factory=list)


class MemorySnapshot(DomainModel):
    """Everything the memory holds; also the readable export/restore format."""

    schema_version: int = MEMORY_SCHEMA_VERSION
    facts: list[CandidateFact] = Field(default_factory=list)
    sources: list[SourceRecord] = Field(default_factory=list)
    conflicts: list[FactConflict] = Field(default_factory=list)
    migrations: list[MigrationRecord] = Field(default_factory=list)

    def fact(self, key: str) -> CandidateFact | None:
        for fact in self.facts:
            if fact.key == key:
                return fact
        return None

    def open_conflict_keys(self) -> set[str]:
        return {c.key for c in self.conflicts if c.status == ConflictStatus.OPEN}


def relative_display(path: Path, root: Path | None) -> str:
    """Store source paths relative to their root so no absolute personal path is needed."""

    if root is not None:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            pass
    return path.name
