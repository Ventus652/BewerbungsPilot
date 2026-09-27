"""The human-readable card of a dossier (README.md), rebuilt from the dossier files.

Same layout as Junior's historical application folders: identity of the offer, match,
honest points, documents, form answers and follow-up. Called after every step (creation,
PDFs, form, submission) so the card is never stale; ``followup.json`` holds facts noted by
hand afterwards (confirmation e-mail, replies, reminders).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")

STATUS = {
    "DISCOVERED": "offre repérée",
    "EVALUATED": "offre évaluée",
    "SELECTED": "retenue — documents à générer",
    "DOCUMENTS_PREPARED": "CV et lettre prêts — formulaire pas encore rempli",
    "FORM_IN_PROGRESS": "formulaire en cours de remplissage",
    "AWAITING_USER": "en attente de ta décision",
    "READY_TO_SUBMIT": "formulaire rempli et vérifié — rien n'est envoyé sans ton « oui »",
    "SUBMITTED": "candidature envoyée — accusé de réception non encore constaté",
    "CONFIRMED": "candidature envoyée et réception confirmée par le portail",
    "FAILED": "échec — voir la raison ci-dessous",
    "REJECTED": "offre écartée par l'évaluation",
    "WITHDRAWN": "dossier retiré",
}

NEXT = {
    "SELECTED": "générer et vérifier les PDF",
    "DOCUMENTS_PREPARED": "remplir le formulaire en mode essai (aucun envoi)",
    "FORM_IN_PROGRESS": "terminer le remplissage",
    "AWAITING_USER": "répondre aux questions du tableau de bord",
    "READY_TO_SUBMIT": "montrer réponses, CV et lettre à Junior ; envoi uniquement sur son « oui » explicite",
    "SUBMITTED": "vérifier le portail et la boîte e-mail (ne jamais recliquer)",
    "CONFIRMED": "surveiller la boîte e-mail ; relancer si aucune réponse après 10 à 14 jours ouvrés",
    "FAILED": "lire la raison de l'échec avant toute nouvelle tentative",
}


def _json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except (OSError, ValueError):
        return default


def _local(iso: str | None) -> str:
    if not iso:
        return "—"
    moment = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(BERLIN)
    return f"{moment:%d.%m.%Y à %H:%M} (Europe/Berlin)"


def _city(text: str | None) -> str:
    return (text.title() if text and text.isupper() else text) or "inconnu"


def build_readme(folder: Path) -> str:
    meta = _json(folder / "meta.json", {})
    app = _json(folder / "application.json", {})
    assessment = _json(folder / "evaluation.json", {}).get("assessment", {})
    extraction = _json(folder / "extraction.json", {}).get("extraction", {})
    documents = _json(folder / "documents.json", {}).get("documents", [])
    plan = _json(folder / "form_plan.json", {}).get("answers", [])
    attempts = _json(folder / "submission_attempts.json", [])
    letter_check = _json(folder / "letter_check.json", {})
    followup = _json(folder / "followup.json", {})
    state = app.get("state", "?")
    receipt = app.get("receipt") or {}

    lines = [f"# Candidature — {meta.get('company', '?')} — {meta.get('title', '?')}", "",
             f"- Entreprise : {meta.get('company', '?')}",
             f"- Poste : {meta.get('title', '?')}",
             f"- Lieu : {_city(meta.get('location'))}",
             f"- Lien officiel : {meta.get('url') or 'à compléter'}"]
    if extraction.get("employment_type"):
        lines.append(f"- Contrat : {extraction['employment_type']}")
    lines += [f"- Date de publication affichée : {extraction.get('publication_date') or 'non indiquée'}",
              f"- Dossier préparé le : {_local(meta.get('created_at'))}"]
    if attempts:
        lines.append(f"- Date de candidature : {_local(attempts[-1].get('clicked_at'))}")
    lines.append(f"- Statut : {STATUS.get(state, state)}")
    if assessment:
        lines.append(f"- Évaluation du moteur : {assessment.get('decision')} ({assessment.get('score')}/100)"
                     + (" — « Candidater quand même » choisi par Junior" if assessment.get("decision") == "REVIEW" and state not in
                        ("EVALUATED", "AWAITING_USER") else ""))

    lines += ["", "## Correspondance", ""]
    lines += [f"- {m['statement']}" for m in assessment.get("matches", [])] or ["- —"]
    if assessment.get("recommended_projects"):
        lines.append(f"- Projets mis en avant : {', '.join(assessment['recommended_projects'])}")

    lines += ["", "## Points à traiter honnêtement", ""]
    honest = [g["statement"] for g in assessment.get("blockers", []) + assessment.get("gaps", []) + assessment.get("unknowns", [])]
    lines += [f"- {s}" for s in honest] or ["- aucun"]
    lines.append("- Ces points ne sont jamais présentés comme une maîtrise dans la lettre ni dans le formulaire.")

    lines += ["", "## Documents", ""]
    for doc in documents:
        lines.append(f"- `{Path(doc.get('path', '')).name}` ({doc.get('document_type', '').lower()}, contrôle : "
                     f"{doc.get('visual_validation', '?')})")
    lines.append(f"- `Motivationsschreiben.md` — contrôle de la lettre : {'OK' if letter_check.get('ok') else 'à revoir'}")
    lines.append("- `Stellenanzeige.md` — texte complet de l'annonce")
    lines.append("- `_previews/` — aperçus des PDF et captures du formulaire")

    if plan:
        lines += ["", "## Formulaire", ""]
        for answer in plan:
            action = answer.get("action")
            if action == "FILL":
                lines.append(f"- {answer['label']} : {answer['value']}")
            elif action == "UPLOAD":
                lines.append(f"- {answer['label']} : fichier `{Path(answer.get('path') or '').name}`")
            elif action == "SKIP":
                lines.append(f"- {answer['label']} : laissé vide (facultatif)")
            else:
                lines.append(f"- {answer['label']} : **question pour Junior** — {answer.get('reason')}")

    lines += ["", "## Suivi", ""]
    if receipt:
        lines.append(f"- Confirmation du portail : {receipt.get('reference', '')[:300]}")
        lines.append(f"- Constatée le : {_local(receipt.get('captured_at'))}")
    for key, label in (("confirmation_email", "E-mail de confirmation"), ("transmitted", "Données transmises"),
                       ("attachments", "Pièces jointes transmises"), ("notes", "Remarques")):
        if followup.get(key):
            lines.append(f"- {label} : {followup[key]}")
    for event in followup.get("events", []):
        lines.append(f"- {event}")
    if app.get("awaiting_reason") and state == "AWAITING_USER":
        lines.append(f"- En attente : {app['awaiting_reason']}")
    if app.get("failure_reason"):
        lines.append(f"- Échec : {app['failure_reason']}")
    lines.append(f"- Prochaine action : {followup.get('next_action') or NEXT.get(state, '—')}")
    lines.append("")
    return "\n".join(lines)


def refresh_readme(folder: Path) -> None:
    """Best effort: a card that cannot be rebuilt must never break the application flow."""
    try:
        (folder / "README.md").write_text(build_readme(folder), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
