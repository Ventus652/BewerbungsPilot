"""Local dashboard (phase 4.4): standard library only, bound to 127.0.0.1.

Security: loopback only, Host header checked (DNS rebinding), every POST needs the
random session token embedded in the page, files are served only from dossier folders and
only as .pdf/.md/.png, journals are already redacted.
"""

from __future__ import annotations

import json
import mimetypes
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from bewerbungspilot.core.journal import read_journal

from .control import RunControl, RunState
from .jobs import JobError, JobManager
from .dossiers import apply_user_action, list_dossiers, save_user_answers

SERVED_SUFFIXES = {".pdf", ".md", ".png"}


class DashboardConfig:
    def __init__(self, app_root: Path, *, port: int = 8765) -> None:
        self.app_root = app_root
        self.dossiers = app_root / "data" / "generated" / "applications"
        self.logs = app_root / "logs"
        self.control = RunControl(app_root / "data" / "generated" / "control.json")
        self.port = port
        self.token = secrets.token_urlsafe(24)
        self.jobs = JobManager(app_root)
        self.restart_requested = False


def _tail(path: Path, n: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return read_journal(path)[-n:]


def make_handler(config: DashboardConfig):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BewerbungsPilot"

        def log_message(self, *args: Any) -> None:  # keep the console quiet
            return

        # ---------------------------------------------------------------- helpers
        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: Any) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _dossier(self, name: str) -> Path | None:
            folder = (config.dossiers / name).resolve()
            if folder.parent != config.dossiers.resolve() or not (folder / "meta.json").exists():
                return None
            return folder

        # ---------------------------------------------------------------- GET
        def do_GET(self) -> None:  # noqa: N802
            if not self._host_ok():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "host refusé"})
            url = urlparse(self.path)
            if url.path == "/":
                html = DASHBOARD_HTML.replace("__TOKEN__", config.token)
                return self._send(HTTPStatus.OK, html.encode("utf-8"), "text/html; charset=utf-8")
            if url.path == "/api/dossiers":
                return self._json(HTTPStatus.OK, list_dossiers(config.dossiers))
            if url.path == "/api/control":
                return self._json(HTTPStatus.OK, {"state": config.control.get().value})
            if url.path == "/api/jobs":
                return self._json(HTTPStatus.OK, config.jobs.list())
            if url.path == "/api/version":
                return self._json(HTTPStatus.OK, {"features": ["jobs", "restart", "select"]})
            if url.path == "/api/journal":
                query = parse_qs(url.query)
                kind = query.get("kind", ["business"])[0]
                if kind not in ("business", "technical"):
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "journal inconnu"})
                n = min(int(query.get("n", ["60"])[0]), 500)
                return self._json(HTTPStatus.OK, _tail(config.logs / kind / f"{kind}.jsonl", n))
            if url.path.startswith("/files/"):
                parts = [unquote(p) for p in url.path.split("/")[2:]]
                if len(parts) not in (2, 3):
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "introuvable"})
                folder = self._dossier(parts[0])
                if folder is None:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "dossier inconnu"})
                target = (folder.joinpath(*parts[1:])).resolve()
                if folder not in target.parents or target.suffix not in SERVED_SUFFIXES or not target.is_file():
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "fichier refusé"})
                content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                if target.suffix == ".md":
                    content_type = "text/plain; charset=utf-8"
                return self._send(HTTPStatus.OK, target.read_bytes(), content_type)
            return self._json(HTTPStatus.NOT_FOUND, {"error": "introuvable"})

        # ---------------------------------------------------------------- POST
        def do_POST(self) -> None:  # noqa: N802
            if not self._host_ok():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "host refusé"})
            if not secrets.compare_digest(self.headers.get("X-BP-Token", ""), config.token):
                return self._json(HTTPStatus.FORBIDDEN, {"error": "jeton manquant ou invalide"})
            length = min(int(self.headers.get("Content-Length") or 0), 10_000)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON invalide"})
            url = urlparse(self.path)
            if url.path == "/api/control":
                try:
                    state = config.control.set(RunState(body.get("state")))
                except ValueError:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "état inconnu"})
                return self._json(HTTPStatus.OK, {"state": state.value})
            if url.path == "/api/jobs":
                try:
                    return self._json(HTTPStatus.OK, config.jobs.start(str(body.get("kind")), dict(body.get("params") or {})))
                except (JobError, ValueError) as exc:
                    return self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            if url.path.startswith("/api/jobs/") and url.path.endswith("/stop"):
                try:
                    return self._json(HTTPStatus.OK, config.jobs.stop(unquote(url.path.split("/")[3])))
                except JobError as exc:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            if url.path == "/api/restart":
                config.restart_requested = True
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return self._json(HTTPStatus.OK, {"restarting": True})
            if url.path.startswith("/api/dossiers/") and url.path.endswith("/answers"):
                folder = self._dossier(unquote(url.path.split("/")[3]))
                if folder is None:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "dossier inconnu"})
                try:
                    return self._json(HTTPStatus.OK, {"answers": save_user_answers(folder, dict(body.get("answers") or {}))})
                except ValueError as exc:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            if url.path.startswith("/api/dossiers/") and url.path.endswith("/action"):
                folder = self._dossier(unquote(url.path.split("/")[3]))
                if folder is None:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "dossier inconnu"})
                try:
                    return self._json(HTTPStatus.OK, apply_user_action(folder, str(body.get("action"))))
                except Exception as exc:  # the state machine explains why
                    return self._json(HTTPStatus.CONFLICT, {"error": f"{type(exc).__name__}: {exc}"})
            return self._json(HTTPStatus.NOT_FOUND, {"error": "introuvable"})

    return Handler


def make_server(config: DashboardConfig) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", config.port), make_handler(config))


DASHBOARD_HTML = r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BewerbungsPilot</title>
<style>
:root{--bg:#f5f7f9;--card:#fff;--ink:#18232f;--muted:#5b6673;--line:#e3e8ee;--accent:#006f72;
--ok:#1e8e5a;--warn:#c77700;--bad:#c0392b;--info:#2f6fb5;--grey:#8a949e}
@media (prefers-color-scheme:dark){:root{--bg:#11161c;--card:#1a2129;--ink:#e7edf3;--muted:#9aa6b2;--line:#2a333d}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,Segoe UI,Arial,sans-serif}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;padding:16px 22px;background:var(--card);border-bottom:1px solid var(--line)}
h1{font-size:19px;margin:0}h1 span{color:var(--accent)}
.controls{display:flex;gap:8px;align-items:center}.pill{padding:4px 10px;border-radius:999px;font-weight:600;font-size:13px;color:#fff}
button{border:1px solid var(--line);background:var(--card);color:var(--ink);padding:7px 12px;border-radius:8px;cursor:pointer;font:inherit}
button:hover{border-color:var(--accent)}button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
main{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1fr);gap:18px;padding:18px 22px}
@media (max-width:900px){main{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:14px}
.card h2{font-size:16px;margin:0 0 2px}.sub{color:var(--muted);font-size:13px}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:8px 0}
.score{font-weight:700}ul{margin:6px 0 0 18px;padding:0}li{margin:2px 0}
.ok li::marker{color:var(--ok)}.risk li::marker{color:var(--warn)}
a{color:var(--accent)}.journal{font-size:13px;max-height:70vh;overflow:auto}
.entry{border-left:3px solid var(--line);padding:4px 8px;margin:6px 0}.entry b{font-weight:600}
.empty{color:var(--muted);padding:20px;text-align:center}
</style></head><body>
<header><h1>🧭 Bewerbungs<span>Pilot</span></h1>
<div class="controls"><span id="run" class="pill" style="background:var(--grey)">…</span>
<button onclick="setRun('PAUSED')">⏸ Pause</button><button onclick="setRun('RUNNING')" class="primary">▶ Reprendre</button>
<button onclick="setRun('STOPPED')">⏹ Arrêter</button></div></header>
<main><section><div class="card"><h2>🚀 Lancer</h2><div class="sub">Colle l'adresse d'une annonce Personio : le système la lit, l'évalue, prépare CV et lettre, remplit le formulaire et <b>s'arrête avant l'envoi</b>.</div>
<div class="row"><input id="url" style="flex:1;min-width:260px;padding:7px;border-radius:8px;border:1px solid var(--line)" placeholder="https://entreprise.jobs.personio.de/job/123456">
<button class="primary" onclick="job('personio_prepare',{url:document.getElementById('url').value})">📝 Préparer (sans envoi)</button></div>
<div class="row"><button onclick="job('tests',{})">🧪 Tests</button><button onclick="job('check_memory',{})">🧠 Contrôle mémoire</button><button onclick="job('diagnose',{})">🩺 Diagnostic Ollama</button><button onclick="restart()">🔄 Redémarrer l'interface</button></div>
<div id="jobs"></div></div><div id="dossiers"></div></section><aside class="card"><h2>📒 Journal métier</h2><div class="sub">événements récents, déjà expurgés</div><div id="journal" class="journal"></div></aside></main>
<script>
const TOKEN="__TOKEN__";
const STATES={DISCOVERED:["Découverte","var(--grey)"],EVALUATED:["Évaluée","var(--info)"],SELECTED:["Retenue","var(--info)"],
DOCUMENTS_PREPARED:["Documents prêts","var(--ok)"],FORM_IN_PROGRESS:["Formulaire","var(--info)"],AWAITING_USER:["À décider","var(--warn)"],
READY_TO_SUBMIT:["Prête à envoyer","var(--ok)"],SUBMITTED:["Envoyée","var(--ok)"],CONFIRMED:["Confirmée","var(--ok)"],
FAILED:["Échec","var(--bad)"],REJECTED:["Rejetée","var(--grey)"],WITHDRAWN:["Retirée","var(--grey)"]};
const RUN={RUNNING:["En marche","var(--ok)"],PAUSED:["En pause","var(--warn)"],STOPPED:["Arrêté","var(--bad)"]};
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
async function post(url,body){const r=await fetch(url,{method:"POST",headers:{"Content-Type":"application/json","X-BP-Token":TOKEN},body:JSON.stringify(body)});
const j=await r.json();if(!r.ok)alert(j.error||"Erreur");return j}
async function setRun(s){await post("/api/control",{state:s});load()}
async function act(name,action){if(action==="withdraw"&&!confirm("Retirer ce dossier ?"))return;await post(`/api/dossiers/${encodeURIComponent(name)}/action`,{action});load()}
function card(d){const [label,color]=STATES[d.state]||[d.state,"var(--grey)"];
const files=d.files.map(f=>`<a href="/files/${encodeURIComponent(d.name)}/${encodeURIComponent(f)}" target="_blank">${f.endsWith(".pdf")?"📄":"📝"} ${esc(f)}</a>`).join(" · ");
const qs=(d.state==="AWAITING_USER"&&d.questions.length)?`<div class="card" style="background:var(--bg)"><b>❓ Questions pour toi</b>${d.questions.map(q=>`<div class="row"><label style="flex:1;min-width:220px">${esc(q.label)}<div class="sub">${esc(q.reason)}</div></label>${q.options.length?`<select data-q="${esc(q.field_id)}"><option value="">—</option>${q.options.map(o=>`<option ${q.answer===o?"selected":""}>${esc(o)}</option>`).join("")}</select>`:`<input data-q="${esc(q.field_id)}" value="${esc(q.answer||"")}" placeholder="ta réponse">`}</div>`).join("")}<div class="row"><button onclick="saveAnswers(this,'${esc(d.name)}')">💾 Enregistrer mes réponses</button></div></div>`:"";
const auth=d.state==="READY_TO_SUBMIT"?`<div class="row">${d.authorized?`<span class="pill" style="background:var(--ok)">Envoi autorisé (une fois)</span><button class="primary" onclick="job('personio_submit',{dossier:'${esc(d.name)}'})">📨 Envoyer maintenant</button>`:`<button class="primary" onclick="authorize('${esc(d.name)}')">✍️ Autoriser l'envoi</button>`}</div>`:"";
const actions=d.state==="AWAITING_USER"?`<div class="row">${d.resume_state==="EVALUATED"?`<button class="primary" onclick="act('${esc(d.name)}','select')">🚀 Candidater quand même</button>`:`<button class="primary" onclick="act('${esc(d.name)}','resume')">✅ Reprendre vers ${esc(d.resume_state)}</button>`}<button onclick="act('${esc(d.name)}','withdraw')">🗑 Retirer</button></div>`:
(["REJECTED","WITHDRAWN","CONFIRMED"].includes(d.state)?"":`<div class="row"><button onclick="act('${esc(d.name)}','withdraw')">🗑 Retirer</button></div>`);
return `<div class="card"><div class="row" style="justify-content:space-between"><div><h2>${esc(d.company)}</h2><div class="sub">${esc(d.title)} · ${esc(d.location||"")}</div></div>
<div class="row"><span class="pill" style="background:${color}">${label}</span><span class="score">${esc(d.decision||"")} ${d.score??""}/100</span></div></div>
${d.awaiting_reason?`<div class="sub">⚠️ ${esc(d.awaiting_reason)}</div>`:""}
<div class="row" style="align-items:flex-start"><div style="flex:1;min-width:220px"><b>Points forts</b><ul class="ok">${d.matches.map(m=>`<li>${esc(m)}</li>`).join("")}</ul></div>
<div style="flex:1;min-width:220px"><b>À vérifier</b><ul class="risk">${(d.risks.length?d.risks:["aucun"]).map(m=>`<li>${esc(m)}</li>`).join("")}</ul></div></div>
<div class="sub">Projets : ${esc(d.projects.join(", ")||"—")}</div><div class="row">${files}</div>${qs}${auth}${actions}</div>`}
async function saveAnswers(btn,name){const box=btn.closest(".card");const answers={};box.querySelectorAll("[data-q]").forEach(el=>{if(el.value)answers[el.dataset.q]=el.value});
await post(`/api/dossiers/${encodeURIComponent(name)}/answers`,{answers});load()}
async function job(kind,params){const r=await post("/api/jobs",{kind,params});if(r&&r.id)loadJobs()}
async function stopJob(id){await post(`/api/jobs/${encodeURIComponent(id)}/stop`,{});loadJobs()}
async function restart(){await post("/api/restart",{});setTimeout(()=>location.reload(),2500)}
async function loadJobs(){const js=await fetch("/api/jobs").then(r=>r.json());const col={running:"var(--info)",done:"var(--ok)",failed:"var(--bad)",stopped:"var(--grey)",stopping:"var(--warn)"};
document.getElementById("jobs").innerHTML=js.map(j=>`<div class="card" style="background:var(--bg)"><div class="row" style="justify-content:space-between"><b>${esc(j.kind)}</b><span class="pill" style="background:${col[j.status]||"var(--grey)"}">${esc(j.status)}</span></div>
<pre style="white-space:pre-wrap;font-size:12px;max-height:260px;overflow:auto;margin:6px 0">${esc(j.output.join("\n"))}</pre>${j.status==="running"?`<button onclick="stopJob('${esc(j.id)}')">⏹ Arrêter la tâche</button>`:""}</div>`).join("")}
setInterval(loadJobs,3000);loadJobs();
async function authorize(name){if(!confirm("Autoriser UN envoi de cette candidature ? Les documents et réponses ont été vérifiés."))return;await post(`/api/dossiers/${encodeURIComponent(name)}/action`,{action:"authorize"});load()}
function jline(e){const when=(e.ts||"").slice(0,19).replace("T"," ");
const what=e.event==="application_transition"?`${esc(e.from_state)} → <b>${esc(e.to_state)}</b> · ${esc(e.reason)}`:
e.event==="offer_decision"?`<b>${esc(e.decision)}</b> · ${esc(e.reason)}`:`<b>${esc(e.event)}</b> ${esc(e.document_type||e.key||"")}`;
return `<div class="entry"><div class="sub">${when} · ${esc((e.run_purpose||"").slice(0,40))}</div>${what}</div>`}
async function load(){const [d,c,j]=await Promise.all([fetch("/api/dossiers").then(r=>r.json()),fetch("/api/control").then(r=>r.json()),fetch("/api/journal?kind=business&n=40").then(r=>r.json())]);
const [l,col]=RUN[c.state];const run=document.getElementById("run");run.textContent=l;run.style.background=col;
document.getElementById("dossiers").innerHTML=d.length?d.map(card).join(""):'<div class="card empty">Aucun dossier. Dépose une annonce dans data\\inbox puis lance le traitement.</div>';
document.getElementById("journal").innerHTML=j.length?j.slice().reverse().map(jline).join(""):'<div class="empty">Journal vide</div>'}
load();setInterval(load,5000);
</script></body></html>"""
