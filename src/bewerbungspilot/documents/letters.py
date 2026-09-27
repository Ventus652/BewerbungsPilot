"""Constrained cover letter from validated facts (phase 4.2).

The letter is assembled by code from a fixed template: every sentence is filled only with
facts from the private memory and elements verified in the offer. No model writes free
text here. The output is then checked by ``documents.checks.check_letter``.
"""

from __future__ import annotations

import re
from datetime import date

from pydantic import BaseModel

from bewerbungspilot.domain.enums import FactCategory
from bewerbungspilot.jobs.evaluation import OfferEvaluation
from bewerbungspilot.jobs.extraction import OfferExtraction
from bewerbungspilot.jobs.fingerprint import _GENDER_MARKERS
from bewerbungspilot.memory.records import MemorySnapshot
from bewerbungspilot.memory.rules import ProfileRules, RuleOutcome

MONTHS_DE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"]
MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
ENGLISH_HINTS = re.compile(r"\b(the|and|you|we|our|your|with|experience|skills|requirements|working student)\b", re.I)
GERMAN_HINTS = re.compile(r"\b(und|die|der|wir|Sie|Ihre|mit|Kenntnisse|Erfahrung|Aufgaben|Werkstudent)\b")


class Letter(BaseModel):
    language: str
    subject: str
    text: str
    facts_used: list[str]
    allowed_numbers_text: str


def offer_language(offer_text: str) -> str:
    return "en" if len(ENGLISH_HINTS.findall(offer_text)) > len(GERMAN_HINTS.findall(offer_text)) else "de"


def _join(items: list[str], word: str) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" {word} " + items[-1]


def _items(evaluation: OfferEvaluation, prefix: str) -> list[str]:
    a = evaluation.assessment
    return [i.statement.split(":", 1)[1].strip().replace(" (bases)", "")
            for i in a.matches + a.gaps if i.statement.startswith(prefix)]


def _skill_names(memory: MemorySnapshot) -> dict[str, str]:
    return {f.key: str(f.value["name"]) for f in memory.facts
            if f.category == FactCategory.SKILL and isinstance(f.value, dict) and "name" in f.value}


AI_TOOLS = {"claude", "chatgpt", "amp", "copilot", "github copilot", "gemini", "llm", "llms", "cursor", "codex"}
METHOD_SKILLS = {"tests", "debogage", "documentation_technique", "git", "github", "gitlab"}
_GENDERED = re.compile(r"(?<=\w)(?::in|\*in|_in|/-?in|\(in\)|:innen|\*innen)\b")
_LEGAL_FORM = re.compile(r"\b(GmbH|AG|SE|UG|KG|KGaA|mbH|e\.\s?V\.)\b")


def clean_title(title: str) -> str:
    title = _GENDER_MARKERS.sub("", title)
    return re.sub(r"\s+", " ", _GENDERED.sub("", title)).strip(" -–")


def with_article(company: str) -> str:
    return f"der {company}" if _LEGAL_FORM.search(company) else company


def project_phrase(name: str, lang: str, first: bool) -> str:
    generic = " / " in name or name.lower().startswith(("data", "game"))
    label = name.replace(" / ", " und ")
    if lang == "de":
        if generic:
            return f"In meinen Projekten im Bereich {label}" if first else f"In Projekten im Bereich {label}"
        return f"In meinem Projekt {name}" if first else f"Im Projekt {name}"
    if generic:
        return f"In my {label} projects" if first else f"In {label} projects"
    return f"In my project {name}" if first else f"In the project {name}"


def _project_techs(memory: MemorySnapshot, project_name: str, offer_first: list[str]) -> list[str]:
    fact = next((f for f in memory.facts if f.category == FactCategory.PROJECT
                 and isinstance(f.value, dict) and f.value.get("name") == project_name), None)
    if fact is None:
        return []
    tags = {t.casefold() for t in fact.tags}
    names = [n for k, n in _skill_names(memory).items()
             if n.casefold() in tags and k.removeprefix("skill.") not in METHOD_SKILLS]
    ordered = [n for n in offer_first if n in names] + [n for n in names if n not in offer_first]
    return ordered[:5]


def build_letter(
    memory: MemorySnapshot,
    evaluation: OfferEvaluation,
    extraction: OfferExtraction,
    offer_text: str,
    *,
    as_of: date,
    include_salary: bool = False,
) -> Letter:
    rules = ProfileRules(memory, as_of=as_of)
    lang = offer_language(offer_text)
    used: list[str] = []

    def value(key: str) -> str | None:
        fact = rules.fact(key)
        if fact is None:
            return None
        used.append(key)
        return str(fact.value)

    name = value("identity.display_name") or ""
    program = (value("education.program") or "Informatik").split(",")[0].strip()
    institution = value("education.institution") or ""
    short = re.search(r"\(([A-ZÄÖÜ]{2,})\)", institution)
    institution_short = short[1] if short else institution.split(",")[0].strip()
    title = clean_title(extraction.title or "") or "Werkstudent"
    company = extraction.company or ""
    city = next((i.statement.split(":", 1)[1].strip() for i in evaluation.assessment.matches
                 if i.statement.startswith("Lieu accepté")), None)
    if city and city.isupper():
        city = city.title()  # "DARMSTADT" as printed by some portals

    required = list(dict.fromkeys(_items(evaluation, "Techno requise documentée") + _items(evaluation, "Piste exigée couverte")))
    preferred_ok = _items(evaluation, "Atout documenté")
    # AI tools are a usage question for the user, never something to "discover" in a letter.
    to_learn = [t for t in _items(evaluation, "Atout non confirmé") if t.casefold() not in AI_TOOLS][:2]
    offer_techs = required + preferred_ok
    projects = evaluation.assessment.recommended_projects[:2]
    cited: set[str] = set()
    used += [f"project:{p}" for p in projects]

    availability = rules.availability()
    hours = rules.weekly_hours()
    bachelor = rules.bachelor_end()
    master = rules.master_intention()
    salary = rules.salary(extraction.title or "") if include_salary else None
    skills = {k.removeprefix("skill."): v for k, v in _skill_names(memory).items()}
    method_words = [w for k, w in (("tests", "Tests"), ("debogage", "Debugging"), ("documentation_technique", "technische Dokumentation")) if k in skills]

    numbers: list[str] = []
    if availability.outcome == RuleOutcome.ALLOW:
        d = availability.value
        numbers += [f"{d.day}. {MONTHS_DE[d.month - 1]} {d.year}", f"{MONTHS_EN[d.month - 1]} {d.day}, {d.year}"]
    if hours.outcome == RuleOutcome.ALLOW:
        numbers.append(f"{hours.value} Stunden {hours.value} hours")
    if bachelor.outcome == RuleOutcome.ALLOW:
        numbers.append(f"{MONTHS_DE[bachelor.value.month - 1]} {bachelor.value.year}")
    if salary is not None and salary.outcome == RuleOutcome.ALLOW:
        numbers.append(f"{salary.value:.2f}".replace(".", ",") + f" {salary.value:.2f}")

    if lang == "de":
        subject = f"Bewerbung als {title}" + (f" – {name}" if name else "")
        p1 = f"mit großem Interesse bewerbe ich mich bei {with_article(company)} als {title}." if company else f"mit großem Interesse bewerbe ich mich als {title}."
        p1 += f" Als {program}student an der {institution_short} möchte ich meine Kenntnisse"
        p1 += f" in {_join(required[:4], 'und')} in Ihr Team einbringen." if required else " in Ihr Team einbringen."
        p2_parts = []
        for index, project in enumerate(projects):
            techs = _project_techs(memory, project, offer_techs)
            if not techs:
                continue
            cited.update(techs)
            if index == 0:
                p2_parts.append(f"{project_phrase(project, 'de', True)} habe ich mit {_join(techs, 'und')} gearbeitet.")
            else:
                p2_parts.append(f"{project_phrase(project, 'de', False)} habe ich außerdem {_join(techs, 'und')} eingesetzt.")
        if method_words:
            p2_parts.append(f"Dabei gehören {_join(method_words, 'und')} selbstverständlich zu meiner Arbeitsweise.")
        p3 = ""
        if required:
            # Already listed in p1: do not repeat the same technologies twice.
            p3 = "Ihre Anforderungen passen gut zu den Schwerpunkten meines Studiums und meiner Projekte."
        extra = [t for t in preferred_ok if t not in cited][:3]
        if extra:
            p3 += f" Auch mit {_join(extra, 'und')} habe ich bereits gearbeitet."
        if to_learn:
            p3 += f" {_join(to_learn, 'und')} möchte ich in Ihrem Team gezielt kennenlernen."
        p4 = ""
        if availability.outcome == RuleOutcome.ALLOW and hours.outcome == RuleOutcome.ALLOW:
            d = availability.value
            p4 = f"Ab dem {d.day}. {MONTHS_DE[d.month - 1]} {d.year} stehe ich für {hours.value} Stunden pro Woche zur Verfügung"
            p4 += f" und kann regelmäßig in {city} arbeiten." if city else "."
        if bachelor.outcome == RuleOutcome.ALLOW:
            p4 += f" Mein Bachelorstudium schließe ich voraussichtlich im {MONTHS_DE[bachelor.value.month - 1]} {bachelor.value.year} ab"
            p4 += "; anschließend plane ich ein Masterstudium." if master.outcome == RuleOutcome.ALLOW and "master" in str(master.value).lower() else "."
        if salary is not None and salary.outcome == RuleOutcome.ALLOW:
            p4 += " Meine Gehaltsvorstellung liegt bei " + f"{salary.value:.2f}".replace(".", ",") + " € brutto pro Stunde."
        closing_line = "Über die Einladung zu einem persönlichen Gespräch freue ich mich sehr."
        salutation, closing = "Sehr geehrte Damen und Herren,", "Mit freundlichen Grüßen"
    else:
        subject = f"Application as {title}" + (f" – {name}" if name else "")
        p1 = f"I am applying for the position of {title}" + (f" at {company}." if company else ".")
        p1 += f" As a computer science student at {institution_short}, I would like to contribute my knowledge of"
        p1 += f" {_join(required[:4], 'and')} to your team." if required else " software development to your team."
        p2_parts = []
        for index, project in enumerate(projects):
            techs = _project_techs(memory, project, offer_techs)
            if techs:
                cited.update(techs)
                p2_parts.append(f"{project_phrase(project, 'en', index == 0)}, I worked with {_join(techs, 'and')}.")
        if method_words:
            p2_parts.append("Testing, debugging and technical documentation are part of how I work." if len(method_words) == 3 else "")
        p3 = "Your requirements match the focus of my studies and projects well." if required else ""
        if to_learn:
            p3 += f" I would like to get to know {_join(to_learn, 'and')} in your team."
        p4 = ""
        if availability.outcome == RuleOutcome.ALLOW and hours.outcome == RuleOutcome.ALLOW:
            d = availability.value
            p4 = f"From {MONTHS_EN[d.month - 1]} {d.day}, {d.year}, I am available for {hours.value} hours per week"
            p4 += f" and can work regularly in {city}." if city else "."
        if bachelor.outcome == RuleOutcome.ALLOW:
            p4 += f" I expect to complete my Bachelor's degree in {MONTHS_EN[bachelor.value.month - 1]} {bachelor.value.year}"
            p4 += " and plan to continue with a Master's degree." if master.outcome == RuleOutcome.ALLOW else "."
        closing_line = "I would be pleased to introduce myself in a personal interview."
        salutation, closing = "Dear Sir or Madam,", "Kind regards"
        numbers.append(f"{MONTHS_EN[bachelor.value.month - 1]} {bachelor.value.year}" if bachelor.outcome == RuleOutcome.ALLOW else "")

    for key in ("availability.start_date", "availability.hours_per_week", "education.bachelor_expected_end", "education.master_intention"):
        used.append(key)
    paragraphs = [p1, " ".join(x for x in p2_parts if x), p3.strip(), p4.strip(), closing_line]
    body = "\n\n".join(p for p in paragraphs if p)
    text = f"# {subject}\n\n{salutation}\n\n{body}\n\n{closing}\n\n{name}\n"
    return Letter(language=lang, subject=subject, text=text, facts_used=list(dict.fromkeys(used)),
                  allowed_numbers_text=" ".join(numbers) + " " + (extraction.title or ""))
