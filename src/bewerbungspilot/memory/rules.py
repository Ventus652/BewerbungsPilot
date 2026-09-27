"""Deterministic rules that must not depend on a model (guide 3.7).

The *rules* live here; the personal *values* (dates, hours, salary, places) are read from
the private memory. When a value is missing or uncertain, the answer is AWAITING_USER,
never a guess.
"""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum
from typing import Any, Iterable

from pydantic import Field

from bewerbungspilot.domain.enums import ApplicationState, DisclosurePolicy as P, DocumentType
from bewerbungspilot.domain.models import Application, CandidateFact, DomainModel

from .parsing import slug
from .records import MemorySnapshot


class RuleOutcome(StrEnum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    AWAITING_USER = "AWAITING_USER"
    UNKNOWN = "UNKNOWN"


class RuleDecision(DomainModel):
    outcome: RuleOutcome
    value: Any = None
    reason: str
    fact_keys: list[str] = Field(default_factory=list)


class Violation(DomainModel):
    rule: str
    excerpt: str


CEFR = re.compile(r"\b(?:A1|A2|B1|B2|C1|C2)\b")
RESIDENCE_TERMS = re.compile(
    r"aufenthalts(?:titel|erlaubnis)|titre de s[ée]jour|residence permit|\bvisa\b|work permit|arbeitserlaubnis",
    re.IGNORECASE,
)
_CLAIM_PREFIXES = re.compile(
    r"^(?:expertise|expérience professionnelle avancée en|maîtrise avancée de|expertise en)\s+",
    re.IGNORECASE,
)
LEARNING_WORDS = re.compile(
    r"kennenlernen|kennen zu lernen|lernen|einarbeiten|vertiefen|aneignen|learn|deepen|get to know|découvrir|apprendre",
    re.IGNORECASE,
)
EXPERT_WORDS = re.compile(r"\b(expert|expertise|experte|senior|fortgeschritten|advanced|maîtrise avancée)\b", re.I)

SOFTWARE_WORDS = ("software", "entwicklung", "development", "developer", "engineer", "programm", "backend", "frontend", "web", "app")

WEEKLY_HOURS = "availability.hours_per_week"
START_DATE = "availability.start_date"
SALARY = "compensation.hourly_gross_eur"
BACHELOR_END = "education.bachelor_expected_end"
MASTER = "education.master_intention"
ADDRESS = "contact.postal_address"
PERMIT_UNTIL = "legal.residence_permit_valid_until"
PERMIT_RENEWED = "legal.residence_permit_renewal_confirmed"
LOCATIONS = "preference.onsite_locations"
REMOTE = "preference.remote_hybrid"
FORBIDDEN_CLAIMS = "action_rule.forbidden_claims"


class ProfileRules:
    def __init__(self, memory: MemorySnapshot, *, as_of: date) -> None:
        self.memory = memory
        self.as_of = as_of
        self._open = memory.open_conflict_keys()

    # ------------------------------------------------------------------ helpers
    def fact(self, key: str) -> CandidateFact | None:
        fact = self.memory.fact(key)
        if fact is None or key in self._open or not fact.is_current(self.as_of):
            return None
        return fact

    def _value(self, key: str) -> Any:
        fact = self.fact(key)
        return None if fact is None else fact.model_dump(mode="json")["value"]

    def _missing(self, key: str, what: str) -> RuleDecision:
        return RuleDecision(outcome=RuleOutcome.AWAITING_USER, reason=f"{what} inconnu ou à vérifier", fact_keys=[key])

    # ------------------------------------------------------------------ availability & conditions
    def weekly_hours(self, requested: int | None = None) -> RuleDecision:
        hours = self._value(WEEKLY_HOURS)
        if hours is None:
            return self._missing(WEEKLY_HOURS, "Temps de travail")
        if requested is not None and requested > int(hours):
            return RuleDecision(
                outcome=RuleOutcome.AWAITING_USER,
                value=hours,
                reason=f"Le poste demande {requested} h/semaine, au-delà des {hours} h validées",
                fact_keys=[WEEKLY_HOURS],
            )
        return RuleDecision(outcome=RuleOutcome.ALLOW, value=int(hours), reason="Heures validées", fact_keys=[WEEKLY_HOURS])

    def availability(self) -> RuleDecision:
        start = self._value(START_DATE)
        if start is None:
            return self._missing(START_DATE, "Date de disponibilité")
        return RuleDecision(outcome=RuleOutcome.ALLOW, value=date.fromisoformat(start), reason="Disponibilité validée", fact_keys=[START_DATE])

    def salary(self, job_kind: str) -> RuleDecision:
        """The validated amount applies only to the job kind it was validated for."""

        fact = self.fact(SALARY)
        if fact is None:
            return self._missing(SALARY, "Prétention salariale")
        text = job_kind.casefold()
        scope = {t.casefold() for t in fact.tags}
        checks = {
            "werkstudent": any(w in text for w in ("werkstudent", "working student")),
            "software": any(w in text for w in SOFTWARE_WORDS),
        }
        missing = sorted(tag for tag in scope if tag in checks and not checks[tag])
        if missing:
            return RuleDecision(
                outcome=RuleOutcome.AWAITING_USER,
                reason=f"Prétention validée pour un autre type de poste (manque : {', '.join(missing)})",
                fact_keys=[SALARY],
            )
        return RuleDecision(outcome=RuleOutcome.ALLOW, value=float(fact.value), reason="Prétention validée pour ce type de poste", fact_keys=[SALARY])

    def bachelor_end(self) -> RuleDecision:
        end = self._value(BACHELOR_END)
        if end is None:
            return self._missing(BACHELOR_END, "Fin prévisionnelle du Bachelor")
        return RuleDecision(outcome=RuleOutcome.ALLOW, value=date.fromisoformat(end), reason="Fin prévisionnelle validée", fact_keys=[BACHELOR_END])

    def master_intention(self) -> RuleDecision:
        value = self._value(MASTER)
        if value is None:
            return self._missing(MASTER, "Projet de Master")
        return RuleDecision(outcome=RuleOutcome.ALLOW, value=value, reason="Intention validée", fact_keys=[MASTER])

    def location(self, city: str | None, *, remote_or_hybrid: bool = False) -> RuleDecision:
        if remote_or_hybrid and self._value(REMOTE):
            return RuleDecision(outcome=RuleOutcome.ALLOW, reason="Télétravail/hybride accepté", fact_keys=[REMOTE])
        places = self._value(LOCATIONS) or []
        if city and any(slug(p) in slug(city) or slug(city) in slug(p) for p in places):
            return RuleDecision(outcome=RuleOutcome.ALLOW, reason="Lieu accepté", fact_keys=[LOCATIONS])
        return RuleDecision(outcome=RuleOutcome.AWAITING_USER, reason=f"Lieu « {city} » hors zone validée", fact_keys=[LOCATIONS])

    # ------------------------------------------------------------------ disclosure
    def disclose(self, key: str, *, explicitly_requested: bool) -> RuleDecision:
        fact = self.memory.fact(key)
        if fact is None:
            return RuleDecision(outcome=RuleOutcome.UNKNOWN, reason="Fait absent : ne pas deviner", fact_keys=[key])
        if key in self._open or not fact.is_current(self.as_of):
            return RuleDecision(outcome=RuleOutcome.AWAITING_USER, reason="Fait en conflit, expiré ou à vérifier", fact_keys=[key])
        policy = fact.effective_policy
        if policy == P.NEVER_EXPORT:
            return RuleDecision(outcome=RuleOutcome.BLOCK, reason="Stockage interne uniquement", fact_keys=[key])
        if not explicitly_requested and (fact.is_sensitive or policy in (P.FORM_REQUIRED_ONLY, P.ASK_BEFORE_USE)):
            return RuleDecision(outcome=RuleOutcome.BLOCK, reason="Jamais mentionné spontanément", fact_keys=[key])
        if policy == P.ASK_BEFORE_USE:
            return RuleDecision(outcome=RuleOutcome.AWAITING_USER, reason="Confirmation humaine requise avant usage", fact_keys=[key])
        return RuleDecision(outcome=RuleOutcome.ALLOW, value=fact.model_dump(mode="json")["value"], reason=f"Politique {policy}", fact_keys=[key])

    def mention_in_document(self, key: str, document: DocumentType | str) -> RuleDecision:
        """CV, letters and e-mails never carry legal facts; the full address only in forms."""

        fact = self.memory.fact(key)
        if fact is not None and (fact.is_sensitive or key.startswith("legal.")):
            return RuleDecision(outcome=RuleOutcome.BLOCK, reason="Information légale jamais mentionnée dans un document", fact_keys=[key])
        if key == ADDRESS:
            return RuleDecision(outcome=RuleOutcome.BLOCK, reason="Adresse complète réservée aux formulaires qui l'exigent", fact_keys=[key])
        return self.disclose(key, explicitly_requested=False)

    # ------------------------------------------------------------------ work authorisation
    def work_authorization(self, valid_through: date) -> RuleDecision:
        """Answer only whether authorisation covers ``valid_through``; never guess a renewal."""

        until = self._value(PERMIT_UNTIL)
        if until is None:
            return self._missing(PERMIT_UNTIL, "Validité de l'autorisation")
        if valid_through <= date.fromisoformat(until):
            return RuleDecision(
                outcome=RuleOutcome.ALLOW,
                value=True,
                reason="Période couverte par la validité actuelle",
                fact_keys=[PERMIT_UNTIL],
            )
        if self._value(PERMIT_RENEWED) is True:
            return RuleDecision(outcome=RuleOutcome.ALLOW, value=True, reason="Renouvellement confirmé", fact_keys=[PERMIT_UNTIL, PERMIT_RENEWED])
        return RuleDecision(
            outcome=RuleOutcome.AWAITING_USER,
            reason="Question au-delà de la validité actuelle et renouvellement non confirmé",
            fact_keys=[PERMIT_UNTIL, PERMIT_RENEWED],
        )

    # ------------------------------------------------------------------ truthful claims
    def forbidden_claim_terms(self) -> list[str]:
        items = self._value(FORBIDDEN_CLAIMS) or []
        terms = []
        for item in items:
            cleaned = _CLAIM_PREFIXES.sub("", str(item)).strip()
            if cleaned and len(cleaned) <= 30 and not cleaned.lower().startswith("toute"):
                terms.append(cleaned)
        return terms

    def claim_skill(self, name: str, *, expert: bool = False) -> RuleDecision:
        for term in self.forbidden_claim_terms():
            if slug(term) and (slug(term) == slug(name) or slug(term) in slug(name).split("_")):
                return RuleDecision(outcome=RuleOutcome.BLOCK, reason=f"« {term} » ne peut pas être déclaré sans preuve", fact_keys=[FORBIDDEN_CLAIMS])
        fact = self.fact(f"skill.{slug(name, 40)}")
        if fact is None:
            return RuleDecision(outcome=RuleOutcome.UNKNOWN, value=False, reason="Compétence non documentée : jamais une expertise", fact_keys=[f"skill.{slug(name, 40)}"])
        if expert:
            return RuleDecision(outcome=RuleOutcome.BLOCK, reason="Ne pas transformer une compétence en expertise", fact_keys=[fact.key])
        return RuleDecision(outcome=RuleOutcome.ALLOW, value=fact.value, reason="Compétence documentée", fact_keys=[fact.key])

    def language_claim(self, language_key: str, cefr: str | None = None) -> RuleDecision:
        fact = self.fact(language_key)
        if fact is None:
            return RuleDecision(outcome=RuleOutcome.UNKNOWN, reason="Langue non documentée", fact_keys=[language_key])
        if cefr and cefr.upper() not in str(fact.value).upper():
            return RuleDecision(outcome=RuleOutcome.BLOCK, reason="Niveau CECR non documenté : ne pas l'inventer", fact_keys=[language_key])
        return RuleDecision(outcome=RuleOutcome.ALLOW, value=fact.value, reason="Formulation documentée", fact_keys=[language_key])

    def check_text(self, text: str, document: DocumentType | str = DocumentType.COVER_LETTER) -> list[Violation]:
        """Scan a generated CV/letter/e-mail for claims the memory does not support."""

        violations: list[Violation] = []
        documented_cefr = {
            code
            for fact in self.memory.facts
            if fact.key.startswith("language.")
            for code in CEFR.findall(str(fact.value))
        }
        for match in CEFR.finditer(text):
            if match[0] not in documented_cefr:
                violations.append(Violation(rule="no_invented_cefr", excerpt=match[0]))
        sentences = [x for x in re.split(r"(?<=[.!?])\s+|\n+", text) if x.strip()]
        for term in self.forbidden_claim_terms():
            pattern = re.compile(rf"(?<![\w]){re.escape(term)}(?![\w])", re.IGNORECASE)
            for sentence in sentences:
                # Wanting to learn a technology is honest; claiming it is not.
                if pattern.search(sentence) and not LEARNING_WORDS.search(sentence):
                    violations.append(Violation(rule="forbidden_claim", excerpt=term))
                    break
        for match in EXPERT_WORDS.finditer(text):
            violations.append(Violation(rule="expertise_claim", excerpt=match[0]))
        if match := RESIDENCE_TERMS.search(text):
            violations.append(Violation(rule="residence_permit_mentioned", excerpt=match[0]))
        permit = self.memory.fact(PERMIT_UNTIL)
        if permit is not None and permit.value:
            permit_date = date.fromisoformat(str(permit.value))
            for spelling in (permit_date.isoformat(), permit_date.strftime("%d.%m.%Y")):
                if spelling in text:
                    violations.append(Violation(rule="residence_permit_date", excerpt=spelling))
        address = self.memory.fact(ADDRESS)
        if address is not None and address.value:
            street = str(address.value).split(",")[0].strip()
            if street and street in text:
                violations.append(Violation(rule="full_address_outside_form", excerpt="adresse postale"))
        return violations

    # ------------------------------------------------------------------ success requires proof
    @staticmethod
    def submission_successful(application: Application) -> bool:
        """Success only with the receipt the Application model already requires."""

        return application.state == ApplicationState.CONFIRMED and application.receipt is not None


def blocking_unknowns(decisions: Iterable[RuleDecision]) -> list[str]:
    """Feed ``TransitionContext.blocking_unknowns`` from rule decisions."""

    return [
        f"{','.join(d.fact_keys)}: {d.reason}"
        for d in decisions
        if d.outcome in (RuleOutcome.AWAITING_USER, RuleOutcome.UNKNOWN)
    ]
