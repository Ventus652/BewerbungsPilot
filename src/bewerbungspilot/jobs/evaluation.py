"""Deterministic offer evaluation (phase 4.1): the model extracts, the code judges.

Every criterion gives points, a verdict and the evidence it relied on — one sentence of the
offer and the memory fact keys. The decision follows fixed rules:

* any blocker → REJECT (score ≤ 30);
* an important unknown or a missing required technology → REVIEW (45–79);
* otherwise → APPLY (≥ 80).

The result is a ``MatchAssessment`` validated by the domain model.
"""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from bewerbungspilot.domain.enums import ApplicationState, FactCategory, MatchDecision
from bewerbungspilot.domain.models import AssessmentItem, MatchAssessment
from bewerbungspilot.memory.context import rank_projects
from bewerbungspilot.memory.records import MemorySnapshot
from bewerbungspilot.memory.rules import ProfileRules, RuleOutcome

from .extraction import HeuristicFacts, VerifiedExtraction, heuristic_facts, parse_hours
from bewerbungspilot.memory.parsing import slug

from .normalize import (
    clean_technology,
    expand_technology,
    looks_like_technology,
    normalize_groups,
    requires_expertise,
    skill_slug,
    split_technologies,
)

ENGINE_VERSION = "offer-eval-v1"
WEIGHTS = {"technologies": 50, "hours": 15, "location": 15, "contract": 10, "start": 10}
PREFERRED_MISSING_PENALTY = 3
GENERIC_WORDS = frozenset({"apis", "api", "services", "service", "kenntnisse", "erfahrung", "basics", "grundlagen"})
PREFERRED_MISSING_MAX = 9

LANGUAGE_KEYS = {
    "deutsch": "language.allemand", "german": "language.allemand", "allemand": "language.allemand",
    "englisch": "language.anglais", "english": "language.anglais", "anglais": "language.anglais",
    "französisch": "language.francais", "french": "language.francais", "français": "language.francais",
}
CEFR = re.compile(r"\b(A1|A2|B1|B2|C1|C2)\b", re.I)


class Verdict(StrEnum):
    OK = "OK"
    GAP = "GAP"
    UNKNOWN = "UNKNOWN"
    BLOCKER = "BLOCKER"


class CriterionResult(BaseModel):
    criterion: str
    points: int
    max_points: int
    verdict: Verdict
    explanation: str


class OfferEvaluation(BaseModel):
    assessment: MatchAssessment
    criteria: list[CriterionResult]
    removed_from_extraction: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    engine_version: str = ENGINE_VERSION

    @property
    def next_state(self) -> ApplicationState:
        return NEXT_STATE[self.assessment.decision]


NEXT_STATE = {
    MatchDecision.APPLY: ApplicationState.SELECTED,
    MatchDecision.REVIEW: ApplicationState.AWAITING_USER,
    MatchDecision.REJECT: ApplicationState.REJECTED,
}


class _Collector:
    def __init__(self) -> None:
        self.matches: list[AssessmentItem] = []
        self.gaps: list[AssessmentItem] = []
        self.blockers: list[AssessmentItem] = []
        self.unknowns: list[AssessmentItem] = []
        self.important_unknown = False

    def add(self, bucket: str, statement: str, *refs: str) -> None:
        item = AssessmentItem(statement=statement, source_refs=[r for r in refs if r] or ["offer:text"])
        getattr(self, bucket).append(item)


def _offer_ref(sentence: str | None) -> str:
    return f"offer:{sentence}" if sentence else "offer:text"


def _sentence_with(term: str, text: str) -> str | None:
    index = text.casefold().find(term.casefold())
    if index < 0:
        return None
    left = max(text.rfind(".", 0, index), text.rfind(";", 0, index)) + 1
    ends = [i for i in (text.find(".", index + len(term)), text.find(";", index + len(term))) if i != -1]
    return text[left:(min(ends) if ends else len(text))].strip()[:160]


def evaluate_offer(
    verified: VerifiedExtraction,
    offer_text: str,
    memory: MemorySnapshot,
    *,
    as_of: date,
    job_offer_id: UUID | None = None,
) -> OfferEvaluation:
    extraction = verified.extraction
    facts = heuristic_facts(offer_text)
    rules = ProfileRules(memory, as_of=as_of)
    out = _Collector()
    criteria: list[CriterionResult] = []
    notes: list[str] = []

    groups = [list(g) for g in extraction.alternative_requirement_groups]
    required = list(extraction.technologies_required)
    if facts.any_of_requirements and not groups and required:
        # The offer asks for ONE track among several but the extraction flattened them:
        # every required technology becomes its own alternative, none is mandatory alone.
        groups, required = [[t] for t in required], []
        notes.append("Exigences alternatives détectées (« au moins une piste ») : aucune techno seule n'est obligatoire")
    criteria.append(_technologies(required, extraction.technologies_mentioned, offer_text, memory, rules, out, groups=groups))
    criteria.append(_hours(extraction.weekly_hours, facts, rules, out))
    criteria.append(_location(extraction.location, facts, rules, out))
    criteria.append(_contract(facts, out))
    criteria.append(_start(facts, rules, out, notes))
    _languages(extraction.required_languages, offer_text, rules, out)
    _enrollment(facts, rules, out)

    raw_score = sum(c.points for c in criteria)
    missing_preferred = [c for c in criteria if c.criterion == "technologies"][0].explanation.count("souhaitée absente")
    raw_score -= min(PREFERRED_MISSING_MAX, PREFERRED_MISSING_PENALTY * missing_preferred)

    required_gap = any(i.statement.startswith("Techno requise non documentée") for i in out.gaps)
    if out.blockers:
        decision = MatchDecision.REJECT
        score = min(raw_score, 30)
    elif out.important_unknown or required_gap:
        decision = MatchDecision.REVIEW
        score = min(max(raw_score, 45), 79)
    else:
        decision = MatchDecision.APPLY
        score = max(raw_score, 80)
    if out.gaps or out.blockers or out.unknowns:
        score = min(score, 99)
    if out.matches:
        score = max(score, 1)
    if decision == MatchDecision.REVIEW and not (out.gaps or out.blockers or out.unknowns):
        out.add("unknowns", "Point à vérifier par l'utilisateur", "offer:text")

    query = " ".join(extraction.technologies_required + extraction.technologies_mentioned + [extraction.title or ""])
    ranked = rank_projects((f for f in memory.facts if f.category == FactCategory.PROJECT), query)
    projects = [f.value["name"] for f, score_ in ranked if score_ > 0 and isinstance(f.value, dict)][:2]

    assessment = MatchAssessment(
        id=uuid4(),
        job_offer_id=job_offer_id or uuid4(),
        score=score,
        decision=decision,
        matches=out.matches,
        gaps=out.gaps,
        blockers=out.blockers,
        unknowns=out.unknowns,
        recommended_projects=projects,
        prompt_version=ENGINE_VERSION,
        model_used="deterministic-rules",
    )
    if verified.removed:
        notes.append(f"{len(verified.removed)} élément(s) de l'extraction rejeté(s) faute de preuve dans le texte")
    notes.append("Offre non vérifiée comme toujours active : contrôle humain avant envoi")
    return OfferEvaluation(assessment=assessment, criteria=criteria, removed_from_extraction=verified.removed, notes=notes)


# --------------------------------------------------------------------------- criteria
def _technologies(required: list[str], mentioned: list[str], text: str, memory: MemorySnapshot,
                  rules: ProfileRules, out: _Collector, *, groups: list[list[str]] | None = None) -> CriterionResult:
    forbidden = {skill_slug(t): t for t in rules.forbidden_claim_terms()}
    groups = normalize_groups(groups or [])
    required_parts = [p for item in required for p in expand_technology(item)]
    expertise = {p for item in required if requires_expertise(item) for p in expand_technology(item)}
    in_tracks = {skill_slug(p) for g in groups for p in g}
    required_keys = {skill_slug(p) for p in required_parts} | in_tracks
    preferred_parts = [p for item in mentioned for p in expand_technology(item)
                       if skill_slug(p) not in required_keys and looks_like_technology(p)]
    covered = 0
    lines = []

    def lookup(name: str):
        raw = slug(clean_technology(name), 40)
        words = [w for w in raw.split("_") if len(w) >= 3 and w not in GENERIC_WORDS]
        keys = list(dict.fromkeys([skill_slug(name), raw, *words]))
        for key in keys:
            if (fact := rules.fact(f"skill.{key}")) is not None:
                return fact
        for key in keys:  # a word inside a longer skill name, e.g. "rest" in "api_rest"
            for candidate in memory.facts:
                if candidate.key.startswith("skill.") and key in candidate.key.removeprefix("skill.").split("_"):
                    return rules.fact(candidate.key)
        return None

    def is_forbidden(name: str) -> str | None:
        key = skill_slug(name)
        for f_key, term in forbidden.items():
            if f_key and (f_key == key or f_key in key.split("_") or key in f_key.split("_")):
                return term
        return None

    for name in dict.fromkeys(required_parts):
        ref = _offer_ref(_sentence_with(name, text))
        term = is_forbidden(name)
        fact = lookup(name)
        if term and fact is None:
            out.add("blockers", f"Techno requise non déclarable sans preuve : {name}", ref, "fact:action_rule.forbidden_claims")
            lines.append(f"{name} : bloquant")
        elif fact is not None and name in expertise:
            out.add("blockers", f"Expertise exigée en {name} : jamais revendiquée", ref, f"fact:{fact.key}")
            lines.append(f"{name} : expertise exigée")
        elif fact is not None:
            covered += 1
            level = fact.value.get("level") if isinstance(fact.value, dict) else None
            suffix = " (bases)" if level == "basics" else ""
            out.add("matches", f"Techno requise documentée : {name}{suffix}", ref, f"fact:{fact.key}")
            lines.append(f"{name} : OK{suffix}")
        else:
            out.add("gaps", f"Techno requise non documentée : {name}", ref, f"fact:skill.{skill_slug(name)}")
            lines.append(f"{name} : absente")
    for name in dict.fromkeys(preferred_parts):
        ref = _offer_ref(_sentence_with(name, text))
        fact = lookup(name)
        if fact is not None and not is_forbidden(name):
            out.add("matches", f"Atout documenté : {name}", ref, f"fact:{fact.key}")
            lines.append(f"{name} (atout) : OK")
        else:
            out.add("gaps", f"Atout non confirmé : {name}", ref, f"fact:skill.{skill_slug(name)}")
            lines.append(f"{name} : souhaitée absente")
    group_units = 0
    group_covered = 0.0
    if groups:
        group_units = 1
        scored = []
        for group in groups:
            parts = list(group)
            documented = [p for p in parts if lookup(p) is not None and not is_forbidden(p)]
            scored.append((len(documented) / len(parts) if parts else 0.0, group, parts, documented))
        ratio, best, parts, documented = max(scored, key=lambda item: item[0])
        label = " / ".join(best)
        if ratio >= 0.5:
            group_covered = 1.0
            for name in documented:
                fact = lookup(name)
                out.add("matches", f"Piste exigée couverte ({label}) : {name}", _offer_ref(_sentence_with(name, text)), f"fact:{fact.key}")
            for name in [p for p in parts if p not in documented]:
                out.add("gaps", f"Atout non confirmé : {name}", _offer_ref(_sentence_with(name, text)), f"fact:skill.{skill_slug(name)}")
            lines.append(f"piste « {label} » : couverte ({len(documented)}/{len(parts)})")
        else:
            group_covered = ratio
            out.add("gaps", f"Techno requise non documentée : aucune piste suffisamment couverte (meilleure : {label})",
                    _offer_ref(_sentence_with(best[0], text)), "fact:skills")
            lines.append(f"aucune piste couverte (meilleure « {label} » {len(documented)}/{len(parts)})")
        others = [" / ".join(g) for g in groups if g is not best]
        if others:
            lines.append("pistes alternatives non retenues : " + "; ".join(others))
    total = len(dict.fromkeys(required_parts))
    if total or group_units:
        points = round(WEIGHTS["technologies"] * (covered + group_covered) / (total + group_units))
    else:
        points = 35
        out.add("unknowns", "Aucune techno explicitement exigée", "offer:text")
    verdict = Verdict.OK if (total or group_units) and covered == total and group_covered == (1.0 if group_units else 0.0) else (Verdict.BLOCKER if any("bloquant" in l or "expertise" in l for l in lines) else Verdict.GAP)
    return CriterionResult(criterion="technologies", points=points, max_points=WEIGHTS["technologies"], verdict=verdict,
                           explanation="; ".join(lines) or "aucune techno")


def _hours(extracted: str | None, facts: HeuristicFacts, rules: ProfileRules, out: _Collector) -> CriterionResult:
    model_min, model_max = parse_hours(extracted)
    rule_min, rule_max = facts.hours_min, facts.hours_max
    ref = _offer_ref(facts.evidence.get("hours") or facts.evidence.get("full_time"))
    if rule_max is not None and model_max is not None and (rule_min, rule_max) != (model_min, model_max):
        out.add("unknowns", f"Heures incohérentes entre lecture et extraction ({rule_min}–{rule_max} vs {model_min}–{model_max})", ref)
        out.important_unknown = True
        return CriterionResult(criterion="hours", points=7, max_points=15, verdict=Verdict.UNKNOWN, explanation="désaccord modèle/règles")
    low = rule_min if rule_min is not None else model_min
    if facts.full_time and (low is None or low > 20):
        low = low or 40
    if low is None:
        out.add("unknowns", "Nombre d'heures hebdomadaires non indiqué", _offer_ref(facts.evidence.get("hours_negotiable")))
        out.important_unknown = True
        return CriterionResult(criterion="hours", points=7, max_points=15, verdict=Verdict.UNKNOWN, explanation="heures inconnues")
    decision = rules.weekly_hours(low)
    if decision.outcome == RuleOutcome.ALLOW:
        out.add("matches", f"Heures compatibles ({low} h minimum, {decision.value} h validées)", ref, "fact:availability.hours_per_week")
        return CriterionResult(criterion="hours", points=15, max_points=15, verdict=Verdict.OK, explanation=decision.reason)
    if decision.outcome == RuleOutcome.AWAITING_USER and decision.value is not None:
        out.add("blockers", f"{low} h/semaine exigées, au-delà des {decision.value} h validées", ref, "fact:availability.hours_per_week")
        return CriterionResult(criterion="hours", points=0, max_points=15, verdict=Verdict.BLOCKER, explanation=decision.reason)
    out.add("unknowns", "Heures validées inconnues dans la mémoire", ref, "fact:availability.hours_per_week")
    out.important_unknown = True
    return CriterionResult(criterion="hours", points=7, max_points=15, verdict=Verdict.UNKNOWN, explanation=decision.reason)


def _location(location: str | None, facts: HeuristicFacts, rules: ProfileRules, out: _Collector) -> CriterionResult:
    ref = _offer_ref(facts.evidence.get("onsite_only") or facts.evidence.get("hybrid") or location)
    if not location:
        out.add("unknowns", "Lieu non indiqué", ref)
        out.important_unknown = True
        return CriterionResult(criterion="location", points=7, max_points=15, verdict=Verdict.UNKNOWN, explanation="lieu inconnu")
    city = re.split(r"[(/,]", location)[0].strip()
    in_zone = rules.location(city).outcome == RuleOutcome.ALLOW
    if in_zone:
        out.add("matches", f"Lieu accepté : {city}", ref, "fact:preference.onsite_locations")
        return CriterionResult(criterion="location", points=15, max_points=15, verdict=Verdict.OK, explanation=f"{city} dans la zone")
    if facts.onsite_only:
        out.add("blockers", f"Présentiel obligatoire hors zone : {city}", ref, "fact:preference.onsite_locations")
        return CriterionResult(criterion="location", points=0, max_points=15, verdict=Verdict.BLOCKER, explanation=f"{city} hors zone, présentiel")
    out.add("unknowns", f"{city} hors zone de présentiel : part de télétravail à vérifier", ref, "fact:preference.onsite_locations")
    out.important_unknown = True
    return CriterionResult(criterion="location", points=5, max_points=15, verdict=Verdict.UNKNOWN, explanation=f"{city} hors zone")


def _contract(facts: HeuristicFacts, out: _Collector) -> CriterionResult:
    if facts.no_student_contract:
        out.add("blockers", "Contrat Werkstudent explicitement exclu", _offer_ref(facts.evidence.get("no_student")), "fact:preference.job_type")
        return CriterionResult(criterion="contract", points=0, max_points=10, verdict=Verdict.BLOCKER, explanation="pas de contrat étudiant")
    if facts.min_years_experience:
        out.add("blockers", f"{facts.min_years_experience} an(s) d'expérience professionnelle exigé(s) : non documenté",
                _offer_ref(facts.evidence.get("years")), "fact:experience")
    if facts.senior:
        out.add("blockers", "Poste senior/lead : hors profil étudiant", _offer_ref(facts.evidence.get("senior")), "fact:preference.job_type")
    if facts.full_time and not facts.student_contract:
        out.add("blockers", "Temps plein sans contrat étudiant", _offer_ref(facts.evidence.get("full_time")), "fact:preference.job_type")
        return CriterionResult(criterion="contract", points=0, max_points=10, verdict=Verdict.BLOCKER, explanation="temps plein")
    if facts.student_contract and not (facts.senior or facts.min_years_experience):
        out.add("matches", "Contrat compatible avec le statut étudiant", _offer_ref(facts.evidence.get("student")), "fact:preference.job_type")
        return CriterionResult(criterion="contract", points=10, max_points=10, verdict=Verdict.OK, explanation="contrat étudiant")
    if facts.senior or facts.min_years_experience:
        return CriterionResult(criterion="contract", points=0, max_points=10, verdict=Verdict.BLOCKER, explanation="expérience/séniorité exigée")
    out.add("unknowns", "Type de contrat non précisé", "offer:text")
    out.important_unknown = True
    return CriterionResult(criterion="contract", points=5, max_points=10, verdict=Verdict.UNKNOWN, explanation="contrat inconnu")


def _start(facts: HeuristicFacts, rules: ProfileRules, out: _Collector, notes: list[str]) -> CriterionResult:
    available = rules.availability()
    ref = _offer_ref(facts.evidence.get("start") or facts.evidence.get("start_unknown"))
    if facts.start_date is None:
        out.add("unknowns", "Date de début non indiquée", ref)
        if facts.start_unknown_stated:
            out.important_unknown = True
        return CriterionResult(criterion="start", points=7, max_points=10, verdict=Verdict.UNKNOWN, explanation="début inconnu")
    if available.outcome != RuleOutcome.ALLOW:
        out.add("unknowns", "Disponibilité non validée dans la mémoire", ref, "fact:availability.start_date")
        out.important_unknown = True
        return CriterionResult(criterion="start", points=5, max_points=10, verdict=Verdict.UNKNOWN, explanation=available.reason)
    if facts.start_date >= available.value:
        out.add("matches", f"Début au {facts.start_date:%d.%m.%Y} compatible avec la disponibilité", ref, "fact:availability.start_date")
        return CriterionResult(criterion="start", points=10, max_points=10, verdict=Verdict.OK, explanation="début compatible")
    out.add("gaps", f"Début souhaité au {facts.start_date:%d.%m.%Y}, avant la disponibilité", ref, "fact:availability.start_date")
    notes.append("Date de début antérieure à la disponibilité : à négocier")
    return CriterionResult(criterion="start", points=5, max_points=10, verdict=Verdict.GAP, explanation="début trop tôt")


def _languages(required: list[str], text: str, rules: ProfileRules, out: _Collector) -> None:
    for requirement in required:
        lowered = requirement.casefold()
        key = next((v for k, v in LANGUAGE_KEYS.items() if k in lowered), None)
        ref = _offer_ref(_sentence_with(requirement.split(":")[0].strip(), text))
        if key is None:
            out.add("unknowns", f"Langue exigée non reconnue : {requirement}", ref)
            continue
        cefr = CEFR.search(requirement)
        decision = rules.language_claim(key, cefr[1].upper() if cefr else None)
        if decision.outcome == RuleOutcome.ALLOW:
            out.add("matches", f"Langue documentée : {requirement}", ref, f"fact:{key}")
        elif decision.outcome == RuleOutcome.BLOCK:
            out.add("unknowns", f"Niveau {cefr[1].upper()} exigé non documenté par un certificat : {requirement}", ref, f"fact:{key}")
            out.important_unknown = True
        else:
            out.add("gaps", f"Langue exigée non documentée : {requirement}", ref, f"fact:{key}")


def _enrollment(facts: HeuristicFacts, rules: ProfileRules, out: _Collector) -> None:
    if not facts.enrollment_required:
        return
    ref = _offer_ref(facts.evidence.get("enrollment"))
    if rules.fact("education.enrollment_status") is not None:
        out.add("matches", "Immatriculation exigée : statut étudiant documenté", ref, "fact:education.enrollment_status")
    else:
        out.add("unknowns", "Immatriculation exigée : statut non documenté", ref, "fact:education.enrollment_status")
        out.important_unknown = True
