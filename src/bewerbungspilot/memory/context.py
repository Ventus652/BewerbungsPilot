"""Minimal context service (guide 3.6).

A task receives only the facts it needs, built by code from current, sourced facts. Every
packet lists the fact identifiers and sources it contains, what was withheld and why, and
whether the task must stop for the user.
"""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum
from typing import Any, Iterable
from uuid import UUID

from pydantic import Field, JsonValue

from bewerbungspilot.domain.enums import DisclosurePolicy as P, FactCategory as C
from bewerbungspilot.domain.models import CandidateFact, DomainModel

from .records import MemorySnapshot


class TaskType(StrEnum):
    OFFER_ANALYSIS = "OFFER_ANALYSIS"
    COVER_LETTER = "COVER_LETTER"
    STANDARD_FORM = "STANDARD_FORM"
    LEGAL_QUESTION = "LEGAL_QUESTION"
    GITHUB_REVIEW = "GITHUB_REVIEW"


# Categories each task may see without an explicit request.
TASK_CATEGORIES: dict[TaskType, frozenset[C]] = {
    TaskType.OFFER_ANALYSIS: frozenset({C.SKILL, C.EDUCATION, C.AVAILABILITY, C.PREFERENCE, C.PROJECT, C.LANGUAGE, C.EXPERIENCE}),
    TaskType.COVER_LETTER: frozenset({C.EDUCATION, C.PROJECT, C.SKILL, C.AVAILABILITY, C.LANGUAGE, C.EXPERIENCE}),
    TaskType.STANDARD_FORM: frozenset(),  # only requested keys
    TaskType.LEGAL_QUESTION: frozenset(),  # only requested keys
    TaskType.GITHUB_REVIEW: frozenset({C.PROJECT, C.SKILL}),
}
# Individual keys a task may see in addition to its categories.
TASK_EXTRA_KEYS: dict[TaskType, frozenset[str]] = {
    TaskType.OFFER_ANALYSIS: frozenset({"contact.city"}),
    TaskType.COVER_LETTER: frozenset({"identity.display_name", "contact.city"}),
    TaskType.GITHUB_REVIEW: frozenset({"identity.github"}),
    TaskType.STANDARD_FORM: frozenset(),
    TaskType.LEGAL_QUESTION: frozenset(),
}
# Policies visible without explicit request. NEVER_EXPORT preferences stay internal but the
# local offer analysis needs them to judge the fit.
DEFAULT_POLICIES = frozenset({P.PUBLIC_PROFILE, P.APPLICATION_STANDARD})
INTERNAL_KEYS_FOR_ANALYSIS = frozenset({"preference.domains", "preference.other_formats"})

TOPIC_SYNONYMS: dict[str, frozenset[str]] = {
    "java": frozenset({"java", "backend", "vert.x", "rest", "api", "mariadb", "sql", "mqtt", "server"}),
    "backend": frozenset({"java", "backend", "vert.x", "rest", "api", "mariadb", "sql"}),
    "frontend": frozenset({"frontend", "react", "typescript", "web", "javascript", "html", "css"}),
    "web": frozenset({"web", "react", "typescript", "frontend", "javascript"}),
    "react": frozenset({"react", "typescript", "frontend", "web"}),
    "typescript": frozenset({"typescript", "react", "frontend", "web"}),
    "data": frozenset({"data", "python", "pandas", "numpy", "machine", "learning", "ml", "pytorch", "cnn", "prévisions", "analyse"}),
    "python": frozenset({"python", "data", "pandas", "pytorch"}),
    "ml": frozenset({"ml", "machine", "learning", "pytorch", "cnn", "python", "data"}),
    "ai": frozenset({"ai", "ki", "ml", "machine", "learning", "pytorch", "cnn", "python"}),
    "ki": frozenset({"ai", "ki", "ml", "machine", "learning", "pytorch", "cnn", "python"}),
    "vision": frozenset({"vision", "cnn", "pytorch", "classification", "images"}),
    "game": frozenset({"game", "godot", "gdscript"}),
}


def query_terms(text: str) -> set[str]:
    words = {w for w in re.split(r"[^a-z0-9#+.äöüß]+", text.casefold()) if len(w) >= 2}
    expanded = set(words)
    for word in words:
        expanded |= TOPIC_SYNONYMS.get(word, frozenset())
    if "machine" in words and "learning" in words:
        expanded |= TOPIC_SYNONYMS["ml"]
    return expanded


class ContextFact(DomainModel):
    id: UUID
    key: str
    value: JsonValue
    source: str
    validated_at: str | None
    sensitive: bool = False


class WithheldItem(DomainModel):
    key: str
    reason: str


class ContextPacket(DomainModel):
    task: TaskType
    as_of: date
    facts: list[ContextFact] = Field(default_factory=list)
    ranked_projects: list[str] = Field(default_factory=list)
    unknown_keys: list[str] = Field(default_factory=list)
    withheld: list[WithheldItem] = Field(default_factory=list)
    awaiting_user: bool = False
    awaiting_reasons: list[str] = Field(default_factory=list)

    @property
    def fact_ids(self) -> list[UUID]:
        return [f.id for f in self.facts]

    @property
    def sources(self) -> list[str]:
        return sorted({f.source for f in self.facts})

    def keys(self) -> set[str]:
        return {f.key for f in self.facts}

    def value(self, key: str) -> Any:
        return next((f.value for f in self.facts if f.key == key), None)

    def to_model_payload(self) -> dict[str, Any]:
        """Compact view handed to a model: values only, no internal metadata."""

        return {
            "task": self.task.value,
            "facts": {f.key: f.value for f in self.facts},
            "projects_in_priority_order": self.ranked_projects,
            "unknown": self.unknown_keys,
        }


def rank_projects(facts: Iterable[CandidateFact], query: str) -> list[tuple[CandidateFact, int]]:
    terms = query_terms(query)
    scored = []
    for fact in facts:
        if fact.category != C.PROJECT:
            continue
        tags = {t.casefold() for t in fact.tags}
        scored.append((fact, len(terms & tags)))
    return sorted(scored, key=lambda item: (-item[1], item[0].key))


def _skill_relevant(fact: CandidateFact, terms: set[str]) -> bool:
    return bool(terms & {t.casefold() for t in fact.tags})


def build_context(
    memory: MemorySnapshot,
    task: TaskType,
    *,
    as_of: date,
    query: str = "",
    requested_keys: Iterable[str] = (),
    max_projects: int = 2,
) -> ContextPacket:
    requested = list(dict.fromkeys(requested_keys))
    open_conflicts = memory.open_conflict_keys()
    by_key = {f.key: f for f in memory.facts}
    packet = ContextPacket(task=task, as_of=as_of)
    selected: list[CandidateFact] = []

    def withhold(key: str, reason: str) -> None:
        packet.withheld.append(WithheldItem(key=key, reason=reason))

    def usable(fact: CandidateFact) -> str | None:
        if fact.key in open_conflicts:
            return "conflit ouvert non résolu"
        if not fact.is_current(as_of):
            return "fait non courant (inconnu, expiré ou à vérifier)"
        return None

    # 1. Explicitly requested keys (forms, legal questions, or a letter asked for a value).
    for key in requested:
        fact = by_key.get(key)
        if fact is None:
            packet.unknown_keys.append(key)
            continue
        if reason := usable(fact):
            packet.unknown_keys.append(key)
            withhold(key, reason)
            continue
        policy = fact.effective_policy
        if policy == P.NEVER_EXPORT:
            withhold(key, "politique NEVER_EXPORT")
        elif fact.is_sensitive and task != TaskType.LEGAL_QUESTION:
            withhold(key, "fait sensible : utiliser une tâche LEGAL_QUESTION")
            packet.awaiting_reasons.append(f"{key} est sensible et demande un traitement explicite")
        elif policy == P.ASK_BEFORE_USE:
            withhold(key, "politique ASK_BEFORE_USE : confirmation humaine requise")
            packet.awaiting_reasons.append(f"{key} exige une confirmation avant usage")
        else:
            selected.append(fact)

    # 2. Default view of the task.
    categories = TASK_CATEGORIES[task]
    extras = TASK_EXTRA_KEYS[task]
    terms = query_terms(query)
    ranked = rank_projects((f for f in memory.facts if f.category == C.PROJECT and usable(f) is None), query)
    if task == TaskType.COVER_LETTER:
        chosen_projects = [f for f, score in ranked if score > 0][:max_projects]
    else:
        chosen_projects = [f for f, _ in ranked]
    packet.ranked_projects = [f.value["name"] for f in chosen_projects if isinstance(f.value, dict)]
    for fact in memory.facts:
        if fact in selected:
            continue
        if fact.category not in categories and fact.key not in extras:
            continue
        if usable(fact) is not None or fact.is_sensitive or fact.category == C.LEGAL_SENSITIVE:
            continue
        if fact.category == C.PROJECT and fact not in chosen_projects:
            continue
        if task == TaskType.COVER_LETTER and fact.category == C.SKILL and not _skill_relevant(fact, terms):
            continue
        policy = fact.effective_policy
        allowed = policy in DEFAULT_POLICIES or (
            task == TaskType.OFFER_ANALYSIS and fact.key in INTERNAL_KEYS_FOR_ANALYSIS
        )
        if allowed:
            selected.append(fact)

    for fact in selected:
        packet.facts.append(
            ContextFact(
                id=fact.id,
                key=fact.key,
                value=fact.model_dump(mode="json")["value"],
                source=fact.source or "unknown",
                validated_at=fact.validated_at.isoformat() if fact.validated_at else None,
                sensitive=fact.is_sensitive,
            )
        )
    if packet.unknown_keys and task in (TaskType.STANDARD_FORM, TaskType.LEGAL_QUESTION):
        packet.awaiting_reasons.append("valeurs demandées inconnues : " + ", ".join(packet.unknown_keys))
    packet.awaiting_user = bool(packet.awaiting_reasons)
    return packet
