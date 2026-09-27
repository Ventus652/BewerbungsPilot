"""Structured local journals (phase 2.6).

Two separate JSON Lines journals, both rotated locally and both scrubbed by the shared
``redact`` filter before writing:

* technical journal — module, event, duration, error, ``run_id``;
* business journal — why an offer was rejected, which document was sent, which receipt
  arrived, every state transition.

Every line carries the ``run_id`` of the execution that produced it, so files and events
can be traced back to one run.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from enum import StrEnum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from .secrets import redact

if TYPE_CHECKING:  # imported lazily to keep ``core`` independent from ``domain``
    from bewerbungspilot.domain.models import DocumentArtifact, SubmissionReceipt
    from bewerbungspilot.domain.state_machine import TransitionRecord

DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RunContext(BaseModel):
    """Identity of one important execution (a search, an application, a diagnostic)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID = Field(default_factory=uuid4)
    purpose: str = Field(min_length=1, max_length=240)
    started_at: AwareDatetime = Field(default_factory=_utc_now)


class JournalKind(StrEnum):
    TECHNICAL = "technical"
    BUSINESS = "business"


def _json_default(value: Any) -> Any:
    if isinstance(value, (UUID, Path)):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return str(value)


class JsonLinesJournal:
    """Append-only, rotated, redacted JSON Lines file."""

    _lock = threading.Lock()

    def __init__(
        self,
        root: str | Path,
        kind: JournalKind,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        backup_count: int = DEFAULT_BACKUP_COUNT,
    ) -> None:
        self.kind = kind
        self.directory = Path(root) / kind.value
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / f"{kind.value}.jsonl"
        self._handler = RotatingFileHandler(
            self.path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger = logging.Logger(f"bewerbungspilot.journal.{kind.value}.{id(self)}")
        self._logger.propagate = False
        self._logger.addHandler(self._handler)

    def write(self, run: RunContext, event: str, **fields: Any) -> dict[str, Any]:
        entry = {
            "ts": _utc_now().isoformat(),
            "journal": self.kind.value,
            "run_id": str(run.run_id),
            "run_purpose": run.purpose,
            "event": event,
            **fields,
        }
        # Serialise first so pydantic objects are plain data, then scrub everything.
        plain = json.loads(json.dumps(entry, default=_json_default, ensure_ascii=False))
        safe = redact(plain)
        with self._lock:
            self._logger.info(json.dumps(safe, ensure_ascii=False, sort_keys=True))
        return safe

    def close(self) -> None:
        self._logger.removeHandler(self._handler)
        self._handler.close()

    def __enter__(self) -> "JsonLinesJournal":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class TechnicalJournal(JsonLinesJournal):
    def __init__(self, root: str | Path = "logs", **kwargs: Any) -> None:
        super().__init__(root, JournalKind.TECHNICAL, **kwargs)

    def record(
        self,
        run: RunContext,
        module: str,
        event: str,
        *,
        duration_seconds: float | None = None,
        error: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        return self.write(
            run,
            event,
            module=module,
            duration_seconds=duration_seconds,
            error=error,
            level="ERROR" if error else "INFO",
            **fields,
        )


class BusinessJournal(JsonLinesJournal):
    def __init__(self, root: str | Path = "logs", **kwargs: Any) -> None:
        super().__init__(root, JournalKind.BUSINESS, **kwargs)

    def record_transition(self, run: RunContext, record: "TransitionRecord") -> dict[str, Any]:
        if record.run_id is not None and record.run_id != run.run_id:
            raise ValueError("TransitionRecord belongs to another run")
        return self.write(run, "application_transition", **record.model_dump(mode="json"))

    def record_offer_decision(
        self, run: RunContext, offer_id: UUID, decision: str, reason: str
    ) -> dict[str, Any]:
        return self.write(run, "offer_decision", offer_id=offer_id, decision=decision, reason=reason)

    def record_document_sent(
        self, run: RunContext, application_id: UUID, document: "DocumentArtifact", channel: str
    ) -> dict[str, Any]:
        return self.write(
            run,
            "document_sent",
            application_id=application_id,
            document_id=document.id,
            document_type=document.document_type,
            sha256=document.sha256,
            version=document.version,
            channel=channel,
        )

    def record_receipt(
        self, run: RunContext, application_id: UUID, receipt: "SubmissionReceipt"
    ) -> dict[str, Any]:
        return self.write(
            run,
            "submission_receipt",
            application_id=application_id,
            kind=receipt.kind,
            reference=receipt.reference,
            captured_at=receipt.captured_at,
            evidence_path=receipt.evidence_path,
        )


def read_journal(path: str | Path) -> list[dict[str, Any]]:
    """Read one journal file back (tests, audits)."""

    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]
