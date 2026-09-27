"""Offer analysis with the minimal context (phase 3 gate).

The model receives only the context packet and the offer; it never reads personal files.
Its answer is then checked by code: a valid JSON is not proof of truth.
"""

from __future__ import annotations

import json
from typing import Any

from bewerbungspilot.llm.types import TextRequest

from .context import ContextPacket
from .rules import ProfileRules

SYSTEM_PROMPT = (
    "Tu évalues une offre d'emploi pour un candidat. Utilise UNIQUEMENT les faits fournis dans "
    "CONTEXTE. N'invente aucune compétence, expérience, date ou projet. Si une exigence n'est pas "
    "couverte par les faits, place-la dans gaps ou missing_information. Ne recommande que des projets "
    "présents dans projects_in_priority_order. Réponds uniquement avec le JSON demandé."
)


def build_offer_request(packet: ContextPacket, offer_text: str, *, case_id: str, run_id: str | None = None) -> TextRequest:
    context = json.dumps(packet.to_model_payload(), ensure_ascii=False, indent=1)
    return TextRequest(
        system=SYSTEM_PROMPT,
        prompt=f"case_id: {case_id}\n\nCONTEXTE:\n{context}\n\nOFFRE:\n{offer_text}",
        run_id=run_id,
    )


def review_model_analysis(parsed: dict[str, Any], packet: ContextPacket, rules: ProfileRules) -> list[str]:
    """Deterministic checks on the model's answer; an empty list means no issue found."""

    issues: list[str] = []
    known_projects = set(packet.ranked_projects)
    for project in parsed.get("recommended_projects", []):
        if project not in known_projects:
            issues.append(f"projet non documenté recommandé : {project}")
    if parsed.get("score") == 0 and parsed.get("factual_matches"):
        issues.append("score nul malgré des correspondances factuelles")
    if parsed.get("decision") == "APPLY" and parsed.get("blockers"):
        issues.append("APPLY malgré un blocage")
    score = parsed.get("score")
    decision = parsed.get("decision")
    blockers = parsed.get("blockers") or []
    if decision == "REJECT" and not blockers:
        issues.append("REJECT sans blocage documenté")
    if isinstance(score, int) and decision == "APPLY" and score < 50:
        issues.append(f"APPLY avec un score faible ({score})")
    if isinstance(score, int) and decision == "REJECT" and score >= 70:
        issues.append(f"REJECT avec un score élevé ({score})")
    if isinstance(score, int) and score < 30 and len(parsed.get("factual_matches") or []) >= 3 and not blockers:
        issues.append(f"score {score} incohérent avec {len(parsed.get('factual_matches') or [])} correspondances et aucun blocage")
    known = packet.keys()
    for field in ("gaps", "missing_information"):
        listed_known = [item for item in parsed.get(field) or [] if str(item).strip() in known]
        if listed_known:
            issues.append(f"{field} contient des faits pourtant fournis : {', '.join(listed_known[:5])}")
    if decision == "REVIEW" and not (parsed.get("gaps") or parsed.get("blockers") or parsed.get("missing_information")):
        issues.append("REVIEW sans lacune, blocage ni inconnue documentés")
    text = " ".join(
        [str(parsed.get("explanation", ""))] + [str(x) for x in parsed.get("factual_matches", [])]
    )
    for violation in rules.check_text(text):
        if violation.rule != "expertise_claim":  # the word may legitimately describe the offer
            issues.append(f"{violation.rule} : {violation.excerpt}")
    return issues
