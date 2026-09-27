"""Cooperative Pause / Resume / Stop for long runs (phase 4.4).

The interface writes the requested state to a small JSON file; the pipeline checks it
between steps — never in the middle of a step, so an action is never cut in half.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path


class RunState(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


class StopRequested(RuntimeError):
    """The user asked to stop; the current step was finished, no new step starts."""


class RunControl:
    def __init__(self, path: str | Path = "data/generated/control.json") -> None:
        self.path = Path(path)

    def get(self) -> RunState:
        try:
            return RunState(json.loads(self.path.read_text(encoding="utf-8"))["state"])
        except (FileNotFoundError, KeyError, ValueError):
            return RunState.RUNNING

    def set(self, state: RunState | str, *, by: str = "user") -> RunState:
        state = RunState(state)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"state": state.value, "changed_at": datetime.now(timezone.utc).isoformat(), "by": by}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp, self.path)
        return state

    def checkpoint(self, *, poll_seconds: float = 1.0, max_wait_seconds: float | None = None) -> None:
        """Call between steps: returns when RUNNING, waits while PAUSED, raises when STOPPED."""

        waited = 0.0
        while True:
            state = self.get()
            if state == RunState.RUNNING:
                return
            if state == RunState.STOPPED:
                raise StopRequested("Arrêt demandé par l'utilisateur")
            if max_wait_seconds is not None and waited >= max_wait_seconds:
                raise TimeoutError("Pause trop longue")
            time.sleep(poll_seconds)
            waited += poll_seconds
