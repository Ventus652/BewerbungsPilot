"""CV plan for one offer: what to reorder and highlight, never what to invent (phase 4.2).

The guide allows changing only the order, the summary and the emphasis of skills and
projects. The plan lists existing facts by key; rendering to PDF comes in step 4.3.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from bewerbungspilot.domain.enums import FactCategory
from bewerbungspilot.jobs.evaluation import OfferEvaluation
from bewerbungspilot.memory.records import MemorySnapshot

from .letters import offer_language


class CvPlan(BaseModel):
    language: str
    headline_skills: list[str]
    project_order: list[str]
    skills_highlighted: list[str]
    skills_basics: list[str]
    show_gpa: bool = False
    fact_keys: list[str] = Field(default_factory=list)
    rules_applied: list[str] = Field(default_factory=list)


def build_cv_plan(memory: MemorySnapshot, evaluation: OfferEvaluation, offer_text: str, *, gpa_requested: bool = False) -> CvPlan:
    a = evaluation.assessment
    project_facts = [f for f in memory.facts if f.category == FactCategory.PROJECT and isinstance(f.value, dict)]

    def profile_position(fact) -> int:  # order of appearance in the reference profile (main project first)
        match = re.search(r":L(\d+)$", fact.source_ref or "")
        return int(match[1]) if match else 10_000

    projects_all = [f.value["name"] for f in sorted(project_facts, key=profile_position)]
    order = [p for p in a.recommended_projects if p in projects_all]
    order += [p for p in projects_all if p not in order and not p.lower().startswith("game")]
    matched_keys = [ref.removeprefix("fact:") for i in a.matches for ref in i.source_refs if ref.startswith("fact:skill.")]
    skill_facts = {f.key: f for f in memory.facts if f.category == FactCategory.SKILL and isinstance(f.value, dict)}
    highlighted = [skill_facts[k].value["name"] for k in dict.fromkeys(matched_keys) if k in skill_facts]
    basics = [f.value["name"] for f in skill_facts.values() if f.value.get("level") == "basics"]
    return CvPlan(
        language=offer_language(offer_text),
        headline_skills=highlighted[:4],
        project_order=order,
        skills_highlighted=highlighted,
        skills_basics=basics,
        show_gpa=gpa_requested,
        fact_keys=list(dict.fromkeys(matched_keys)),
        rules_applied=[
            "CV sur une page ; seuls l'ordre, le résumé et la mise en avant changent",
            "compétences de base affichées comme bases, jamais comme expertise",
            "moyenne affichée seulement si demandée",
            "Game Development seulement si pertinent pour l'annonce",
        ],
    )
