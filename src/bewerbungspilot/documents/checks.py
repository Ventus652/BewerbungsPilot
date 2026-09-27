"""Deterministic checks on a generated letter or e-mail (phase 4.2).

A text is sendable only if every claim can be traced to the fact pack or the offer:

* rule violations from ``ProfileRules.check_text`` (CEFR, forbidden claims, expertise,
  residence permit, full address);
* technologies: a technology not documented in memory may only appear in a sentence about
  learning it, and must come from the offer;
* numbers and dates must exist in the fact pack or the offer;
* no other company than the target may be named (no cross-application leakage);
* e-mail formatting rule: salutation, real paragraphs separated by one blank line, closing
  formula and name on separate lines, no indentation.
"""

from __future__ import annotations

import re
from typing import Iterable

from pydantic import BaseModel, Field

from bewerbungspilot.jobs.normalize import skill_slug
from bewerbungspilot.memory.parsing import slug
from bewerbungspilot.memory.records import MemorySnapshot
from bewerbungspilot.memory.rules import LEARNING_WORDS, ProfileRules

# Common technology names that must never appear unless documented or learning-framed.
KNOWN_TECHNOLOGIES = (
    "Java", "Python", "JavaScript", "TypeScript", "React", "Angular", "Vue", "Node.js", "Spring Boot", "Spring",
    "Kotlin", "C#", ".NET", "C++", "Go", "Rust", "PHP", "Ruby", "Scala", "SQL", "MariaDB", "MySQL", "PostgreSQL",
    "Oracle", "MongoDB", "Redis", "Kafka", "Docker", "Kubernetes", "AWS", "Azure", "GCP", "Terraform", "Jenkins",
    "GitLab", "GitHub", "Git", "Linux", "Vert.x", "REST", "GraphQL", "MQTT", "HTML", "CSS", "Pandas", "NumPy",
    "PyTorch", "TensorFlow", "Jupyter", "KNIME", "Power BI", "Tableau", "SAP", "Salesforce", "Godot", "Selenium",
    "Cypress", "JUnit", "Figma", "Flutter", "Swift",
)
CLOSINGS = ("Mit freundlichen Grüßen", "Kind regards", "Best regards", "Sincerely")
SALUTATION = re.compile(r"^(Sehr geehrte|Guten Tag|Hallo|Dear|Hello)\b.*,$")


class LetterIssue(BaseModel):
    rule: str
    detail: str


class LetterCheck(BaseModel):
    ok: bool
    issues: list[LetterIssue] = Field(default_factory=list)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def _tech_pattern(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w.#+]){re.escape(name)}(?![\w#+]|\.\w)", re.IGNORECASE if len(name) > 3 else 0)


def check_letter(
    text: str,
    *,
    memory: MemorySnapshot,
    rules: ProfileRules,
    offer_text: str,
    company: str | None,
    allowed_numbers_text: str,
    other_companies: Iterable[str] = (),
    signature: str | None = None,
) -> LetterCheck:
    issues: list[LetterIssue] = []

    for violation in rules.check_text(text):
        issues.append(LetterIssue(rule=violation.rule, detail=violation.excerpt))

    issues.extend(undocumented_technologies(text, memory=memory, rules=rules, offer_text=offer_text))

    allowed = allowed_numbers_text + " " + offer_text
    for number in re.findall(r"\b\d+(?:[.,]\d+)?\b", text):
        if not re.search(rf"(?<!\d){re.escape(number)}(?!\d)", allowed):
            issues.append(LetterIssue(rule="unsupported_number", detail=number))

    target = slug(company or "")
    for other in other_companies:
        other_name = re.sub(r"\s*\(.*\)|\b(GmbH|AG|SE|KG|Co\.|&|mbH|Betriebs-KG)\b", "", other or "").strip(" ,.-")
        if len(other_name) < 3 or (target and slug(other_name) in target):
            continue
        if re.search(rf"\b{re.escape(other_name)}\b", text, re.IGNORECASE):
            issues.append(LetterIssue(rule="other_company_named", detail=other_name))

    issues.extend(_format_issues(text, signature))
    return LetterCheck(ok=not issues, issues=issues)


def _format_issues(text: str, signature: str | None) -> list[LetterIssue]:
    issues: list[LetterIssue] = []
    lines = text.strip("\n").split("\n")
    body_lines = [l for l in lines if not l.startswith(("#", "Betreff", "Subject"))]
    if any(l.startswith(("\t", "  ")) and l.strip() for l in body_lines):
        issues.append(LetterIssue(rule="format_indentation", detail="indentation ou tabulation en début de ligne"))
    if "\n\n\n" in text:
        issues.append(LetterIssue(rule="format_blank_lines", detail="plus d'une ligne vide entre deux blocs"))
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]
    blocks = [b for b in blocks if not b.startswith(("#", "Betreff", "Subject"))]
    if not blocks or not SALUTATION.match(blocks[0].splitlines()[0]):
        issues.append(LetterIssue(rule="format_salutation", detail="salutation absente ou sans virgule finale"))
    paragraphs = [b for b in blocks[1:] if not b.startswith(CLOSINGS) and b != signature]
    if len(paragraphs) < 3:
        issues.append(LetterIssue(rule="format_paragraphs", detail=f"{len(paragraphs)} paragraphe(s) : au moins 3 attendus"))
    for paragraph in paragraphs:
        if len(paragraph) > 700:
            issues.append(LetterIssue(rule="format_long_paragraph", detail=paragraph[:60] + "…"))
    closing_index = next((i for i, b in enumerate(blocks) if b.startswith(CLOSINGS)), None)
    if closing_index is None:
        issues.append(LetterIssue(rule="format_closing", detail="formule de politesse absente"))
    elif "\n" in blocks[closing_index] or (signature and (closing_index + 1 >= len(blocks) or blocks[closing_index + 1] != signature)):
        issues.append(LetterIssue(rule="format_signature", detail="formule et nom doivent être sur des lignes distinctes séparées par une ligne vide"))
    return issues
def undocumented_technologies(text: str, *, memory: MemorySnapshot, rules: ProfileRules, offer_text: str = "") -> list[LetterIssue]:
    """Technologies named in ``text`` that the memory does not document (learning-framed offer techs excepted)."""

    issues: list[LetterIssue] = []
    documented = {
        skill_slug(str(f.value.get("name")))
        for f in memory.facts
        if f.key.startswith("skill.") and isinstance(f.value, dict) and rules.fact(f.key) is not None
    } | {f.key.removeprefix("skill.") for f in memory.facts if f.key.startswith("skill.")}
    for sentence in _sentences(text):
        for tech in KNOWN_TECHNOLOGIES:
            if not _tech_pattern(tech).search(sentence):
                continue
            key = skill_slug(tech)
            if key in documented or slug(tech) in documented:
                continue
            if LEARNING_WORDS.search(sentence) and _tech_pattern(tech).search(offer_text):
                continue
            issues.append(LetterIssue(rule="undocumented_technology", detail=f"{tech} : « {sentence[:90]} »"))
    return issues
