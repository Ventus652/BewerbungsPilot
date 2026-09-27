"""Phase 4.4 — run control, local dashboard security, user decisions, inbox pipeline."""

import json
import threading
import time
from datetime import date
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from bewerbungspilot.api.control import RunControl, RunState, StopRequested
from bewerbungspilot.api.dossiers import apply_user_action, list_dossiers
from bewerbungspilot.api.pipeline import run_inbox
from bewerbungspilot.api.server import DashboardConfig, make_server
from bewerbungspilot.core.journal import read_journal
from bewerbungspilot.documents.dossier import create_dossier
from bewerbungspilot.domain import ApplicationState as S
from bewerbungspilot.domain.enums import FactCategory
from bewerbungspilot.jobs.evaluation import evaluate_offer
from bewerbungspilot.jobs.extraction import OfferExtraction, verify_extraction
from bewerbungspilot.memory.benchmark import _fact, memory_from_benchmark_profile
from bewerbungspilot.memory.cv_library import build_library
from bewerbungspilot.memory.records import MemorySnapshot

BENCH = Path(__file__).resolve().parents[2] / "benchmarks"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "memory_root"
TODAY = date(2026, 9, 27)
MEMORY = MemorySnapshot(facts=[*memory_from_benchmark_profile().facts,
                               _fact("identity.display_name", "Alex Beispiel", FactCategory.IDENTITY),
                               _fact("contact.city", "Musterstadt", FactCategory.CONTACT),
                               _fact("education.bachelor_expected_end", "2027-09-30", FactCategory.EDUCATION)])
LIBRARY = build_library(FIXTURES)


def reference(case: str):
    text = json.loads((BENCH / "cases" / f"{case}.json").read_text(encoding="utf-8"))["input"]
    extraction = OfferExtraction.model_validate(json.loads((BENCH / "expected_reviewed" / f"{case}.json").read_text(encoding="utf-8")))
    return text, extraction


def make_dossier(root: Path, case: str):
    text, extraction = reference(case)
    verified = verify_extraction(extraction, text)
    evaluation = evaluate_offer(verified, text, MEMORY, as_of=TODAY)
    return create_dossier(offer_text=text, verified=verified, evaluation=evaluation, memory=MEMORY, root=root, as_of=TODAY)


# ------------------------------------------------------------------ run control
def test_control_defaults_to_running_and_stop_raises(tmp_path) -> None:
    control = RunControl(tmp_path / "control.json")
    assert control.get() == RunState.RUNNING
    control.checkpoint()
    control.set(RunState.STOPPED)
    with pytest.raises(StopRequested):
        control.checkpoint()


def test_pause_blocks_until_resumed(tmp_path) -> None:
    control = RunControl(tmp_path / "control.json")
    control.set(RunState.PAUSED)
    threading.Timer(0.3, lambda: control.set(RunState.RUNNING)).start()
    started = time.monotonic()
    control.checkpoint(poll_seconds=0.05, max_wait_seconds=5)
    assert time.monotonic() - started >= 0.25


# ------------------------------------------------------------------ user decisions
def test_awaiting_dossier_resumes_only_to_its_recorded_state(tmp_path) -> None:
    text, extraction = reference("A02")
    verified = verify_extraction(extraction.model_copy(update={"weekly_hours": "30"}), text)  # model/rules disagree -> REVIEW
    evaluation = evaluate_offer(verified, text, MEMORY, as_of=TODAY)
    dossier = create_dossier(offer_text=text, verified=verified, evaluation=evaluation, memory=MEMORY, root=tmp_path, as_of=TODAY)
    assert dossier.application.state == S.AWAITING_USER
    result = apply_user_action(dossier.folder, "resume")
    assert result["state"] == S.EVALUATED.value
    assert json.loads((dossier.folder / "transitions.json").read_text(encoding="utf-8"))[-1]["actor"] == "USER"


def test_withdraw_and_unknown_action(tmp_path) -> None:
    dossier = make_dossier(tmp_path, "A01")
    with pytest.raises(ValueError):
        apply_user_action(dossier.folder, "submit")
    assert apply_user_action(dossier.folder, "withdraw")["state"] == S.WITHDRAWN.value
    with pytest.raises(Exception):
        apply_user_action(dossier.folder, "withdraw")  # terminal state


def test_list_dossiers_summaries(tmp_path) -> None:
    make_dossier(tmp_path, "A01")
    (summary,) = list_dossiers(tmp_path)
    assert summary["company"] == "Softwerk Mitte GmbH" and summary["state"] == S.SELECTED.value
    assert "Motivationsschreiben.md" in summary["files"]


# ------------------------------------------------------------------ dashboard server
@pytest.fixture
def server(tmp_path):
    app_root = tmp_path / "app"
    (app_root / "data" / "generated" / "applications").mkdir(parents=True)
    config = DashboardConfig(app_root, port=0)
    httpd = make_server(config)
    config.port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield config
    httpd.shutdown()
    httpd.server_close()


def call(config, path, *, method="GET", body=None, token=True, host=None):
    request = Request(f"http://127.0.0.1:{config.port}{path}", method=method,
                      data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    if token:
        request.add_header("X-BP-Token", config.token)
    if host:
        request.add_header("Host", host)
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, exc.read()


def test_dashboard_page_and_api(server) -> None:
    status, body = call(server, "/")
    assert status == 200 and server.token.encode() in body and b"BewerbungsPilot" in body
    assert call(server, "/api/dossiers")[1] == b"[]"


def test_post_requires_token(server) -> None:
    assert call(server, "/api/control", method="POST", body={"state": "PAUSED"}, token=False)[0] == 403
    status, body = call(server, "/api/control", method="POST", body={"state": "PAUSED"})
    assert status == 200 and json.loads(body)["state"] == "PAUSED"
    assert json.loads(call(server, "/api/control")[1])["state"] == "PAUSED"


def test_foreign_host_is_refused(server) -> None:
    assert call(server, "/api/dossiers", host="evil.example")[0] == 403


def test_files_are_confined_to_dossiers(server) -> None:
    dossier = make_dossier(server.dossiers, "A01")
    ok = call(server, f"/files/{dossier.folder.name}/Motivationsschreiben.md")
    assert ok[0] == 200 and b"Sehr geehrte" in ok[1]
    assert call(server, f"/files/{dossier.folder.name}/meta.json")[0] == 404
    assert call(server, f"/files/{dossier.folder.name}/..%2F..%2F..%2Fdata%2Fprivate%2Fprofile.json")[0] == 404
    assert call(server, "/files/..%2F..%2Fprivate/profile.json")[0] == 404


def test_user_action_through_the_api(server) -> None:
    dossier = make_dossier(server.dossiers, "A01")
    status, body = call(server, f"/api/dossiers/{dossier.folder.name}/action", method="POST", body={"action": "withdraw"})
    assert status == 200 and json.loads(body)["state"] == "WITHDRAWN"
    assert call(server, f"/api/dossiers/{dossier.folder.name}/action", method="POST", body={"action": "resume"})[0] == 409


# ------------------------------------------------------------------ inbox pipeline
def fake_extractor(text: str, stem: str) -> OfferExtraction:
    return reference(stem)[1]


def test_pipeline_processes_inbox_once_and_renders_documents(tmp_path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for case in ("A01", "A04"):
        (inbox / f"{case}.md").write_text(reference(case)[0], encoding="utf-8")
    kwargs = dict(inbox=inbox, dossiers=tmp_path / "dossiers", logs=tmp_path / "logs", memory=MEMORY, library=LIBRARY,
                  extractor=fake_extractor, control=RunControl(tmp_path / "control.json"), past_applications=[], as_of=TODAY)
    results = run_inbox(**kwargs)
    by_file = {r["file"]: r for r in results}
    assert by_file["A01.md"]["state"] == S.DOCUMENTS_PREPARED.value
    assert by_file["A04.md"]["status"] == "RejectedOfferError"
    assert run_inbox(**kwargs) == []  # already processed: nothing twice
    events = [e["event"] for e in read_journal(tmp_path / "logs" / "business" / "business.jsonl")]
    assert "offer_decision" in events and "application_transition" in events


def test_pipeline_stops_between_offers(tmp_path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "A01.md").write_text(reference("A01")[0], encoding="utf-8")
    control = RunControl(tmp_path / "control.json")
    control.set(RunState.STOPPED)
    results = run_inbox(inbox=inbox, dossiers=tmp_path / "d", logs=tmp_path / "logs", memory=MEMORY, library=LIBRARY,
                        extractor=fake_extractor, control=control, past_applications=[], as_of=TODAY)
    assert results == [{"file": "A01.md", "status": "STOPPED"}]
    assert not (tmp_path / "d").exists()


def test_user_can_apply_despite_review(tmp_path) -> None:
    """REVIEW → AWAITING_USER → « Candidater quand même » → SELECTED → documents rendered."""
    import shutil
    from bewerbungspilot.api.dossiers import select_despite_review
    from bewerbungspilot.memory.store import MemoryStore
    app_root = tmp_path / "app"
    root = app_root / "data" / "generated" / "applications"
    store = MemoryStore(app_root / "data" / "private")
    store.save(MEMORY)
    store.save_cv_library(LIBRARY)
    text, extraction = reference("A02")
    verified = verify_extraction(extraction.model_copy(update={"weekly_hours": "30"}), text)
    evaluation = evaluate_offer(verified, text, MEMORY, as_of=TODAY)
    dossier = create_dossier(offer_text=text, verified=verified, evaluation=evaluation, memory=MEMORY, root=root, as_of=TODAY)
    assert dossier.application.state == S.AWAITING_USER and dossier.application.resume_state == S.EVALUATED
    result = select_despite_review(dossier.folder)
    assert result["state"] == S.DOCUMENTS_PREPARED.value and result["cv_ok"] and result["letter_ok"]
    transitions = json.loads((dossier.folder / "transitions.json").read_text(encoding="utf-8"))
    assert [t["actor"] for t in transitions][-3:-1] == ["USER", "USER"]


# ------------------------------------------------------------------ dashboard jobs
def test_jobs_are_whitelisted_validated_and_single(tmp_path, monkeypatch) -> None:
    import sys
    from bewerbungspilot.api.jobs import JobError, JobManager
    manager = JobManager(tmp_path)
    with pytest.raises(JobError):
        manager.start("rm_everything", {})
    with pytest.raises(ValueError):
        manager.start("personio_prepare", {"url": "https://evil.example/job/1"})
    dossier = make_dossier(tmp_path / "data" / "generated" / "applications", "A01")
    with pytest.raises(JobError):
        manager.start("personio_submit", {"dossier": dossier.folder.name})  # not authorised by the user
    monkeypatch.setattr(manager, "_command", lambda kind, params: [sys.executable, "-c", "import time; print('hello'); time.sleep(1)"])
    job = manager.start("tests", {})
    with pytest.raises(JobError):
        manager.start("tests", {})  # one at a time
    manager.jobs[job["id"]]["_process"].wait()
    time.sleep(0.3)
    listed = manager.list()[0]
    assert listed["status"] == "done" and "hello" in listed["output"]


def test_restart_endpoint_requires_token(server) -> None:
    assert call(server, "/api/restart", method="POST", body={}, token=False)[0] == 403
    assert call(server, "/api/version")[0] == 200
