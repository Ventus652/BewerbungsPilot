"""Decide every form answer from the memory and the rules — never from a guess (phase 4.5).

Each field gets one action: FILL, UPLOAD, SKIP (optional and unknown) or ASK (the user
must decide). Consent checkboxes, full postal address, uncertain legal questions, official
documents and any unknown required field are always ASK.
"""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field

from bewerbungspilot.domain.enums import DocumentType, VisualValidationStatus
from bewerbungspilot.domain.models import DocumentArtifact
from bewerbungspilot.memory.records import MemorySnapshot
from bewerbungspilot.memory.rules import ProfileRules, RuleOutcome

from .portal import FieldKind, FormField, FormSnapshot


class AnswerAction(StrEnum):
    FILL = "FILL"
    UPLOAD = "UPLOAD"
    SKIP = "SKIP"
    ASK = "ASK"


class PlannedAnswer(BaseModel):
    field_id: str
    label: str
    action: AnswerAction
    value: str | None = None
    path: str | None = None
    fact_keys: list[str] = Field(default_factory=list)
    reason: str
    options: list[str] = Field(default_factory=list)
    kind: str | None = None


class FillPlan(BaseModel):
    portal: str
    answers: list[PlannedAnswer]

    @property
    def questions(self) -> list[PlannedAnswer]:
        return [a for a in self.answers if a.action == AnswerAction.ASK]


class _Context:
    def __init__(self, memory: MemorySnapshot, rules: ProfileRules, documents: list[DocumentArtifact],
                 folder: Path, job_title: str) -> None:
        self.memory, self.rules, self.documents, self.folder, self.job_title = memory, rules, documents, folder, job_title


def _fact(ctx: _Context, field: FormField, key: str, reason: str) -> PlannedAnswer:
    decision = ctx.rules.disclose(key, explicitly_requested=True)
    if decision.outcome == RuleOutcome.ALLOW:
        return PlannedAnswer(field_id=field.id, label=field.label, action=AnswerAction.FILL, value=str(decision.value),
                             fact_keys=[key], reason=reason)
    return _ask(field, f"{key} : {decision.reason}", [key])


def _ask(field: FormField, reason: str, keys: list[str] | None = None) -> PlannedAnswer:
    return PlannedAnswer(field_id=field.id, label=field.label, action=AnswerAction.ASK, fact_keys=keys or [], reason=reason,
                         options=list(field.options), kind=field.kind.value)


def _date_value(field: FormField, value: date) -> str:
    return value.strftime("%d.%m.%Y")


def _option(field: FormField, wanted: set[str]) -> str | None:
    wanted = {w.casefold() for w in wanted}  # casefold on both sides ("ß" becomes "ss")
    for option in field.options:
        if option.casefold() in wanted or any(w in option.casefold() for w in wanted):
            return option
    return None


# --------------------------------------------------------------------------- handlers
def _first_name(ctx, field):
    return _fact(ctx, field, "identity.first_name", "prénom confirmé") if ctx.memory.fact("identity.first_name") else \
        _ask(field, "Répartition prénom / nom de famille non confirmée : à préciser une fois", ["identity.first_name"])


def _last_name(ctx, field):
    return _fact(ctx, field, "identity.last_name", "nom de famille confirmé") if ctx.memory.fact("identity.last_name") else \
        _ask(field, "Répartition prénom / nom de famille non confirmée : à préciser une fois", ["identity.last_name"])


def _gender(ctx, field):
    fact = ctx.rules.fact("identity.form_gender")
    if fact is None:
        return _ask(field, "Genre non documenté", ["identity.form_gender"])
    tokens = {t for t in re.split(r"[^\w]+", str(fact.value).casefold()) if t}
    synonyms = {"masculin": {"männlich", "male", "herr", "mann"}, "male": {"männlich", "male", "herr"},
                "féminin": {"weiblich", "female", "frau"}, "female": {"weiblich", "female", "frau"},
                "divers": {"divers", "diverse", "other"}}
    wanted = set(tokens)
    for token in tokens:
        wanted |= synonyms.get(token, set())
    option = _option(field, wanted) if field.options else str(fact.value)
    if option is None:
        return _ask(field, "Aucune option ne correspond au genre documenté", [fact.key])
    return PlannedAnswer(field_id=field.id, label=field.label, action=AnswerAction.FILL, value=option,
                         fact_keys=[fact.key], reason="genre documenté pour les formulaires")


def _city(ctx, field):
    answer = _fact(ctx, field, "contact.city", "ville validée")
    if answer.action == AnswerAction.FILL and answer.value:
        answer = answer.model_copy(update={"value": answer.value.split(",")[0].strip()})  # "Gießen, Allemagne" → "Gießen"
    return answer


_TECH_QUESTION = re.compile(
    r"(?:experience|erfahrung|kenntnisse|knowledge|skills?)\s+(?:\w+\s+){0,4}?(?:with|in|using|mit|im|der|of)\s+(?:the\s+)?([A-Za-z#+.\-]+(?:\s[A-Za-z#+.]+)?)",
    re.I)


def _tech_experience(ctx, field):
    match = _TECH_QUESTION.search(field.label)
    if not match:
        return _ask(field, "Question de compétence non reconnue")
    tech = re.sub(r"\s+(programming|language|programmiersprache|framework|sprache)$", "", match[1], flags=re.I).strip(" ?.")
    decision = ctx.rules.claim_skill(tech)
    if decision.outcome == RuleOutcome.ALLOW:
        option = _option(field, {"yes", "ja", "oui"}) if field.options else "Ja"
        if option:
            return PlannedAnswer(field_id=field.id, label=field.label, action=AnswerAction.FILL, value=option,
                                 fact_keys=decision.fact_keys, reason=f"compétence documentée : {tech}")
    return _ask(field, f"Compétence « {tech} » : {decision.reason}", decision.fact_keys)


def _motivation_text(ctx, field):
    letter = ctx.folder / "Motivationsschreiben.md"
    if field.kind != FieldKind.TEXTAREA or not letter.exists():
        return _ask(field, "Texte libre demandé : à rédiger ou valider")
    blocks = [b.strip() for b in re.split(r"\n\s*\n", letter.read_text(encoding="utf-8")) if b.strip()]
    body = [b for b in blocks if not b.startswith(("#", "Sehr geehrte", "Dear", "Mit freundlichen", "Kind regards"))][:-2]
    text = "\n\n".join(body[:3])
    text = text[:1].upper() + text[1:]  # the letter's first paragraph follows the salutation in lower case
    return PlannedAnswer(field_id=field.id, label=field.label, action=AnswerAction.FILL, value=text[:2000],
                         fact_keys=["document:Motivationsschreiben.md"], reason="paragraphes de la lettre déjà contrôlée")


def _hours(ctx, field):
    decision = ctx.rules.weekly_hours()
    if decision.outcome != RuleOutcome.ALLOW:
        return _ask(field, decision.reason, decision.fact_keys)
    return PlannedAnswer(field_id=field.id, label=field.label, action=AnswerAction.FILL, value=str(decision.value),
                         fact_keys=decision.fact_keys, reason="heures validées")


def _start(ctx, field):
    decision = ctx.rules.availability()
    if decision.outcome != RuleOutcome.ALLOW:
        return _ask(field, decision.reason, decision.fact_keys)
    return PlannedAnswer(field_id=field.id, label=field.label, action=AnswerAction.FILL,
                         value=_date_value(field, decision.value), fact_keys=decision.fact_keys, reason="disponibilité validée")


def _graduation(ctx, field):
    decision = ctx.rules.bachelor_end()
    if decision.outcome != RuleOutcome.ALLOW:
        return _ask(field, decision.reason, decision.fact_keys)
    return PlannedAnswer(field_id=field.id, label=field.label, action=AnswerAction.FILL,
                         value=_date_value(field, decision.value), fact_keys=decision.fact_keys, reason="fin prévisionnelle validée")


def _salary(ctx, field):
    decision = ctx.rules.salary(ctx.job_title)
    if decision.outcome != RuleOutcome.ALLOW:
        return _ask(field, decision.reason, decision.fact_keys)
    return PlannedAnswer(field_id=field.id, label=field.label, action=AnswerAction.FILL,
                         value=f"{decision.value:.2f}".replace(".", ","), fact_keys=decision.fact_keys, reason="salaire validé pour ce type de poste")


def _work_permit(ctx, field):
    label = field.label
    target = None
    if match := re.search(r"\b(\d{1,2}\.\d{1,2}\.\d{4})\b", label):
        day, month, year = (int(x) for x in match[1].split("."))
        target = date(year, month, day)
    elif match := re.search(r"\b(20\d{2})\b", label):
        target = date(int(match[1]), 12, 31)  # "bis 2027" = the whole year, the strict reading
    if target is None:
        end = ctx.rules.bachelor_end()
        if end.outcome != RuleOutcome.ALLOW:
            return _ask(field, "Période couverte par la question inconnue", ["legal.residence_permit_valid_until"])
        target = end.value
    decision = ctx.rules.work_authorization(target)
    if decision.outcome == RuleOutcome.ALLOW and decision.value is True and (option := _option(field, {"ja", "yes", "oui"})):
        return PlannedAnswer(field_id=field.id, label=field.label, action=AnswerAction.FILL, value=option,
                             fact_keys=decision.fact_keys, reason=decision.reason)
    return _ask(field, f"Question d'autorisation jusqu'au {target:%d.%m.%Y} : {decision.reason}", decision.fact_keys)


def _document(document_type: DocumentType):
    def handler(ctx, field):
        document = next((d for d in ctx.documents if d.document_type == document_type
                         and d.visual_validation == VisualValidationStatus.PASSED), None)
        if document is None or not (ctx.folder / document.path).exists():
            return _ask(field, f"Aucun document {document_type} validé pour cette candidature")
        return PlannedAnswer(field_id=field.id, label=field.label, action=AnswerAction.UPLOAD,
                             path=str(document.path), fact_keys=[f"document:{document.id}"], reason="document validé de ce dossier")
    return handler


_LANGUAGE_LEVELS = [  # documented level (any language of the profile) → option words, strongest first
    (("muttersprach", "maternelle", "native", "c2", "c1", "très bon", "sehr gut", "verhandlungssicher", "fluent", "fließend"),
     ("fließend", "fluent", "verhandlungssicher", "sehr gut", "c1", "courant")),
    (("bon", "gut", "good", "b2", "b1", "fortgeschritten", "advanced"),
     ("fortgeschritten", "advanced", "gut", "good", "b2", "intermediate")),
    (("notion", "grundlagen", "basic", "a1", "a2", "débutant"), ("grundlegend", "basic", "grundkenntnisse", "a2", "beginner")),
]
_LANGUAGE_KEYS = {"deutsch": "language.allemand", "german": "language.allemand", "englisch": "language.anglais",
                  "english": "language.anglais", "französisch": "language.francais", "french": "language.francais"}


def _language(ctx, field):
    label = field.label.casefold()
    key = next((k for word, k in _LANGUAGE_KEYS.items() if word in label), None)
    fact = ctx.rules.fact(key) if key else None
    if fact is None:
        return _ask(field, "Niveau de langue non documenté", [key] if key else [])
    level = str(fact.value).casefold()
    if not field.options:
        return PlannedAnswer(field_id=field.id, label=field.label, action=AnswerAction.FILL, value=str(fact.value),
                             fact_keys=[fact.key], reason="niveau de langue documenté")
    for documented, words in _LANGUAGE_LEVELS:
        if any(d in level for d in documented):
            option = next((o for w in words for o in field.options if w.casefold() in o.casefold()), None)  # ß → ss
            if option:
                return PlannedAnswer(field_id=field.id, label=field.label, action=AnswerAction.FILL, value=option,
                                     fact_keys=[fact.key], reason=f"niveau documenté « {fact.value} » → {option}")
            break
    return _ask(field, f"Niveau documenté « {fact.value} » sans option correspondante", [fact.key])


def _always_ask(reason: str):
    return lambda ctx, field: _ask(field, reason)


RULES: list[tuple[re.Pattern[str], Callable[[_Context, FormField], PlannedAnswer]]] = [
    (re.compile(r"datenschutz|privacy|einwillig|consent|agb|terms|nutzungsbeding|akzeptier", re.I),
     _always_ask("Acceptation de conditions : décision humaine obligatoire")),
    (re.compile(r"how did you (find|hear)|wie sind sie|aufmerksam geworden|source", re.I),
     _always_ask("Origine de l'annonce : à indiquer")),
    (re.compile(r"why .*(fit|apply|interest)|warum|motivation|weshalb|reasons? for", re.I), _motivation_text),
    (re.compile(r"(deutsch|englisch|französisch|german|english|french)\w*\s*(kenntnisse|skills|level|niveau)|sprachkenntnisse|language (skills|level)", re.I), _language),
    (re.compile(r"github|gitlab|portfolio|code-?beispiel|code sample", re.I),
     lambda ctx, field: _fact(ctx, field, "identity.github", "profil GitHub public")),
    (re.compile(r"(experience|erfahrung|kenntnisse|knowledge).*(with|in|using|mit)\b", re.I), _tech_experience),
    (re.compile(r"arbeitserlaubnis|aufenthalt|work permit|visa|residence|autorisation de travail|arbeitsgenehmigung", re.I), _work_permit),
    (re.compile(r"zeugnis|transcript|notenspiegel|leistungs|certificate|nachweis", re.I),
     _always_ask("Document officiel : choisir le fichier à joindre")),
    (re.compile(r"lebenslauf|\bcv\b|resume|curriculum", re.I), (_CV_HANDLER := _document(DocumentType.CV))),
    (re.compile(r"anschreiben|cover letter|motivationsschreiben|lettre", re.I), _document(DocumentType.COVER_LETTER)),
    (re.compile(r"^(other|weitere|sonstige|additional)( documents?| dokumente| unterlagen)?$", re.I), _document(DocumentType.COVER_LETTER)),
    (re.compile(r"vorname|first name|given name|prénom", re.I), _first_name),
    (re.compile(r"nachname|last name|family name|surname|nom de famille", re.I), _last_name),
    (re.compile(r"vollständiger name|full name|^name$|legal name", re.I),
     lambda ctx, field: _fact(ctx, field, "identity.legal_name", "nom légal confirmé")),
    (re.compile(r"e-?mail", re.I), lambda ctx, field: _fact(ctx, field, "contact.email", "e-mail validé")),
    (re.compile(r"telefon|phone|mobil|handy", re.I), lambda ctx, field: _fact(ctx, field, "contact.phone_international", "téléphone validé")),
    (re.compile(r"geschlecht|gender|anrede|genre", re.I), _gender),
    (re.compile(r"straße|strasse|anschrift|adresse|address|plz|postleitzahl|zip", re.I),
     lambda ctx, field: _fact(ctx, field, "contact.postal_address", "adresse")),
    (re.compile(r"wohnort|\bstadt\b|\bcity\b|\bort\b", re.I), _city),
    (re.compile(r"hochschule|universit|university|école", re.I), lambda ctx, field: _fact(ctx, field, "education.institution", "établissement validé")),
    (re.compile(r"studiengang|field of study|programme|major", re.I), lambda ctx, field: _fact(ctx, field, "education.program", "formation validée")),
    (re.compile(r"abschluss|graduation|diplôme", re.I), _graduation),
    (re.compile(r"wochenstunden|stunden pro woche|hours per week|weekly hours|arbeitszeit", re.I), _hours),
    (re.compile(r"eintritt|start|verfügbar|available|beginn|disponibilit", re.I), _start),
    (re.compile(r"gehalt|salary|vergütung|rémunération", re.I), _salary),
]


def plan_answers(snapshot: FormSnapshot, *, memory: MemorySnapshot, as_of: date, documents: list[DocumentArtifact],
                 folder: Path, job_title: str, user_answers: dict[str, str] | None = None) -> FillPlan:
    """``user_answers`` holds the user's explicit decisions for this dossier (field id → value)."""

    ctx = _Context(memory, ProfileRules(memory, as_of=as_of), documents, folder, job_title)
    answers = []
    for field in snapshot.fields:
        if user_answers and field.id in user_answers:
            value = user_answers[field.id]
            if field.options and value not in field.options:
                answers.append(_ask(field, f"Réponse utilisateur « {value} » absente des options {field.options}"))
            else:
                answers.append(PlannedAnswer(field_id=field.id, label=field.label, action=AnswerAction.FILL, value=value,
                                             fact_keys=["user:decision"], reason="décision explicite de l'utilisateur"))
            continue
        if field.kind == FieldKind.CHECKBOX:
            # A checkbox is a declaration (consent, awareness, commitment): always the user's call.
            answers.append(_ask(field, "Case à cocher = déclaration : décision de l'utilisateur"))
            continue
        handler = next((h for pattern, h in RULES if pattern.search(field.label)), None)
        if handler is _CV_HANDLER and field.kind != FieldKind.FILE:
            handler = None
        if handler is None:
            answer = (_ask(field, "Champ obligatoire inconnu : ne jamais deviner") if field.required else
                      PlannedAnswer(field_id=field.id, label=field.label, action=AnswerAction.SKIP, reason="champ facultatif inconnu"))
        else:
            answer = handler(ctx, field)
            if answer.action == AnswerAction.ASK and not field.required and field.kind != FieldKind.CHECKBOX:
                answer = answer.model_copy(update={"action": AnswerAction.SKIP, "reason": f"facultatif ; {answer.reason}"})
        answers.append(answer)
    return FillPlan(portal=snapshot.portal, answers=answers)
