"""Durable storage for raw responses that fail parsing or validation."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from bewerbungspilot.core.secrets import redact


class FailureStore(Protocol):
    def record(
        self, category: str, payload: dict[str, Any], run_id: UUID | None = None
    ) -> Path | None: ...


class FileFailureStore:
    def __init__(self, root: str | Path = "logs/llm_failures") -> None:
        self.root = Path(root)

    def record(
        self, category: str, payload: dict[str, Any], run_id: UUID | None = None
    ) -> Path:
        """Persist the raw response, scrubbed of secrets, and tag it with the run."""

        self.root.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        path = self.root / f"{timestamp}_{category}_{uuid4().hex[:8]}.json"
        document = {
            "category": category,
            "run_id": None if run_id is None else str(run_id),
            "payload": redact(json.loads(json.dumps(payload, default=str))),
        }
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        return path
