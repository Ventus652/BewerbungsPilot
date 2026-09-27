"""Background jobs started from the dashboard (whitelisted scripts only).

Only fixed commands can run, one at a time, with validated arguments. A submission job is
accepted only when the user already authorised that dossier in the dashboard.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from bewerbungspilot.browser.web_portal import personio_apply_url


class JobError(ValueError):
    pass


class JobManager:
    def __init__(self, app_root: Path) -> None:
        self.app_root = app_root
        self.logs = app_root / "logs" / "jobs"
        self.jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ commands
    def _command(self, kind: str, params: dict[str, Any]) -> list[str]:
        python = sys.executable
        script = lambda name: str(self.app_root / "scripts" / name)  # noqa: E731
        if kind == "personio_prepare":
            url = str(params.get("url", "")).strip()
            personio_apply_url(url)  # validates the address
            return [python, script("personio_apply.py"), "--url", url, "--no-wait"]
        if kind == "personio_submit":
            folder = self._dossier(str(params.get("dossier", "")))
            authorization = json.loads((folder / "submission_authorization.json").read_text(encoding="utf-8")) \
                if (folder / "submission_authorization.json").exists() else {}
            if not authorization.get("execution_id") or authorization.get("used"):
                raise JobError("Envoi refusé : aucune autorisation de l'utilisateur pour ce dossier")
            url = json.loads((folder / "meta.json").read_text(encoding="utf-8")).get("url")
            if not url:
                raise JobError("Ce dossier n'a pas d'adresse Personio")
            return [python, script("personio_apply.py"), "--url", url, "--submit", "--no-wait"]
        if kind == "tests":
            return [python, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
        if kind == "check_memory":
            return [python, script("check_private_memory.py")]
        if kind == "diagnose":
            return [python, script("diagnose.py"), "--live"]
        raise JobError(f"Tâche inconnue : {kind}")

    def _dossier(self, name: str) -> Path:
        root = (self.app_root / "data" / "generated" / "applications").resolve()
        folder = (root / name).resolve()
        if folder.parent != root or not (folder / "meta.json").exists():
            raise JobError("Dossier inconnu")
        return folder

    # ------------------------------------------------------------------ lifecycle
    def start(self, kind: str, params: dict[str, Any]) -> dict[str, Any]:
        command = self._command(kind, params)
        with self._lock:
            if any(j["status"] == "running" for j in self.jobs.values()):
                raise JobError("Une tâche est déjà en cours")
            job_id = f"{datetime.now():%Y%m%d_%H%M%S}_{kind}_{uuid4().hex[:4]}"
            self.logs.mkdir(parents=True, exist_ok=True)
            log_path = self.logs / f"{job_id}.log"
            handle = log_path.open("w", encoding="utf-8")
            env = {**__import__("os").environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
            process = subprocess.Popen(command, cwd=self.app_root, stdout=handle, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, env=env)
            job = {"id": job_id, "kind": kind, "params": params, "status": "running", "started": datetime.now().isoformat(),
                   "log": str(log_path), "_process": process, "_handle": handle, "exit_code": None}
            self.jobs[job_id] = job
        threading.Thread(target=self._wait, args=(job,), daemon=True).start()
        return self.public(job)

    def _wait(self, job: dict[str, Any]) -> None:
        code = job["_process"].wait()
        job["_handle"].close()
        job["exit_code"] = code
        job["status"] = "done" if code == 0 else ("stopped" if job["status"] == "stopping" else "failed")

    def stop(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job is None:
            raise JobError("Tâche inconnue")
        if job["status"] == "running":
            job["status"] = "stopping"
            job["_process"].terminate()
        return self.public(job)

    def public(self, job: dict[str, Any], tail: int = 60) -> dict[str, Any]:
        try:
            lines = Path(job["log"]).read_text(encoding="utf-8", errors="replace").splitlines()[-tail:]
        except FileNotFoundError:
            lines = []
        return {k: v for k, v in job.items() if not k.startswith("_")} | {"output": lines}

    def list(self) -> list[dict[str, Any]]:
        return [self.public(j, tail=40) for j in sorted(self.jobs.values(), key=lambda j: j["started"], reverse=True)[:10]]
