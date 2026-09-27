"""Offer extraction: the model proposes, the code verifies (phase 4.1).

``OfferExtraction`` mirrors ``benchmarks/schemas/job_extraction.schema.json``. Anything the
model states must be traceable to the offer text; what cannot be verified is removed and
reported. ``heuristic_facts`` reads the text with plain rules so the model can be
cross-checked — a disagreement becomes an unknown, never an arbitrary choice.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from bewerbungspilot.llm.base import LLMClient
from bewerbungspilot.llm.types import TextRequest
from bewerbungspilot.memory.parsing import parse_date

from .normalize import clean_technology, split_technologies

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "benchmarks" / "schemas" / "job_extraction.schema.json"

EXTRACTION_SYSTEM = (
    "Tu extrais les informations d'une offre d'emploi. Copie les valeurs telles qu'elles "
    "apparaissent dans le texte. N'invente rien, ne complète rien : un champ absent vaut null "
    "et va dans unknown_fields. technologies_required = seulement ce qui est explicitement "
    "exigé ; les atouts ou « von Vorteil » vont uniquement dans technologies_mentioned. "
    "evidence_fragments = courtes citations exactes du texte. Si l'annonce exige seulement UNE piste "
    "parmi plusieurs (« mindestens einem der folgenden Bereiche », « at least one of », « oder »), "
    "mets chaque piste comme une liste dans alternative_requirement_groups et PAS dans "
    "technologies_required. Réponds uniquement en JSON."
)


class OfferExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = "X00"
    company: str | None = None
    title: str | None = None
    location: str | None = None
    employment_type: str | None = None
    weekly_hours: str | None = None
    technologies_required: list[str] = Field(default_factory=list)
    technologies_mentioned: list[str] = Field(default_factory=list)
    required_languages: list[str] = Field(default_factory=list)
    publication_date: str | None = None
    hard_requirements: list[str] = Field(default_factory=list)
    unknown_fields: list[str] = Field(default_factory=list)
    evidence_fragments: list[str] = Field(default_factory=list)
    alternative_requirement_groups: list[list[str]] = Field(default_factory=list)


class VerifiedExtraction(BaseModel):
    extraction: OfferExtraction
    removed: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)


_QUOTES = re.compile("[\"'«»„“”‚‘’`]")


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"[‐-―]", "-", text)
    text = _QUOTES.sub("", text)  # the model often wraps quotations in quote marks
    return re.sub(r"\s+", " ", text)


def appears_in(fragment: str, text: str) -> bool:
    return _norm(fragment).strip(" .,;:") in _norm(text)


def load_schema() -> dict[str, Any]:
    """Benchmark schema, with the date left as free text: the code normalises it.

    Models copy German dates (``22.09.2026``); rejecting the whole answer for that lost two
    valid extractions on 27/09/2026. The frozen benchmark schema file is not modified.
    """

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    schema["properties"]["publication_date"].pop("pattern", None)
    schema["properties"]["alternative_requirement_groups"] = {
        "type": "array", "items": {"type": "array", "items": {"type": "string"}}}
    return schema


def normalize_date_text(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    if match := re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", value):
        try:
            return date(int(match[3]), int(match[2]), int(match[1])).isoformat()
        except ValueError:
            return None
    parsed = parse_date(value)
    return parsed.isoformat() if parsed else None


def extract_with_model(client: LLMClient, offer_text: str, *, case_id: str = "X00", run_id: str | None = None) -> OfferExtraction:
    result = client.generate_structured(
        TextRequest(system=EXTRACTION_SYSTEM, prompt=f"case_id: {case_id}\n\nOFFRE:\n{offer_text}", run_id=run_id),
        load_schema(),
    )
    return OfferExtraction.model_validate(result.parsed)


def verify_extraction(extraction: OfferExtraction, offer_text: str) -> VerifiedExtraction:
    """Keep only what the offer text supports."""

    removed: list[str] = []
    unknowns = list(dict.fromkeys(extraction.unknown_fields))

    def keep_technologies(items: list[str], label: str) -> list[str]:
        kept = []
        for item in items:
            parts = split_technologies(item)
            if parts and all(appears_in(part, offer_text) for part in parts):
                kept.append(item)
            else:
                removed.append(f"{label}: {item!r} absent du texte")
        return kept

    fragments = []
    for fragment in extraction.evidence_fragments:
        if appears_in(fragment, offer_text):
            fragments.append(fragment)
        else:
            removed.append(f"citation inventée ou modifiée : {fragment!r}")

    raw_publication = extraction.publication_date
    publication = normalize_date_text(raw_publication)
    if raw_publication and publication is None:
        removed.append(f"date de publication illisible : {raw_publication!r}")
    if publication:
        try:
            published = date.fromisoformat(publication)
        except ValueError:
            published = None
        textual = {published.strftime("%d.%m.%Y"), published.isoformat(),
                   f"{published.day}.{published.month}.{published.year}"} if published else set()
        if not published or not any(t in offer_text for t in textual):
            removed.append(f"date de publication non trouvée telle quelle : {publication!r}")
            publication = None
    if publication is None and "publication_date" not in unknowns:
        unknowns.append("publication_date")

    groups = []
    for group in extraction.alternative_requirement_groups:
        kept = keep_technologies(group, "techno d'une piste")
        if kept:
            groups.append(kept)
    verified = extraction.model_copy(
        update={
            "alternative_requirement_groups": groups,
            "technologies_required": keep_technologies(extraction.technologies_required, "techno requise"),
            "technologies_mentioned": keep_technologies(extraction.technologies_mentioned, "techno mentionnée"),
            "evidence_fragments": fragments,
            "publication_date": publication,
            "unknown_fields": unknowns,
        }
    )
    return VerifiedExtraction(extraction=verified, removed=removed, unknowns=unknowns)


# --------------------------------------------------------------------------- rule-based reading
_NUMBER_WORDS = {"ein": 1, "einem": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5, "fuenf": 5, "sechs": 6,
                 "sieben": 7, "acht": 8, "zehn": 10, "one": 1, "two": 2, "three": 3, "five": 5}
_HOURS_RANGE = re.compile(r"(\d{1,2})\s*(?:[-–]|bis|to|à)\s*(\d{1,2})\s*(?:std\.?|stunden|h\b|hours|heures)", re.I)
_HOURS_SINGLE = re.compile(r"(?<![\d–-])(\d{1,2})\s*(?:std\.?|stunden|h\b|hours|heures)", re.I)
_YEARS = re.compile(
    r"(?:mindestens|min\.|at least|au moins)\s+(\d+|\w+)\s+(?:jahre?n?|years?|ans)", re.I
)
_START = re.compile(r"(?:beginn|start|starttermin|eintritt)\w*\s*(?:ab|:|zum|am|from)?\s*(\d{1,2}\.\d{1,2}\.\d{4})", re.I)


class HeuristicFacts(BaseModel):
    hours_min: int | None = None
    hours_max: int | None = None
    hours_negotiable: bool = False
    full_time: bool = False
    student_contract: bool = False
    no_student_contract: bool = False
    senior: bool = False
    min_years_experience: int | None = None
    onsite_only: bool = False
    hybrid_or_remote: bool = False
    start_date: date | None = None
    start_unknown_stated: bool = False
    enrollment_required: bool = False
    any_of_requirements: bool = False
    evidence: dict[str, str] = Field(default_factory=dict)


def _sentence(text: str, start: int, end: int) -> str:
    left = max(text.rfind(".", 0, start), text.rfind(";", 0, start), text.rfind("\n", 0, start)) + 1
    candidates = [i for i in (text.find(".", end), text.find(";", end), text.find("\n", end)) if i != -1]
    right = min(candidates) if candidates else len(text)
    return text[left:right].strip()[:160]


def heuristic_facts(offer_text: str) -> HeuristicFacts:
    text = offer_text
    low = _norm(text)
    facts = HeuristicFacts()

    def mark(key: str, match: re.Match[str]) -> None:
        facts.evidence[key] = _sentence(text, match.start(), match.end())

    if match := _HOURS_RANGE.search(text):
        facts.hours_min, facts.hours_max = sorted((int(match[1]), int(match[2])))
        mark("hours", match)
    elif match := _HOURS_SINGLE.search(text):
        facts.hours_min = facts.hours_max = int(match[1])
        mark("hours", match)
    if match := re.search(r"nach absprache|flexible arbeitszeiten|flexible hours|wochenstundenzahl.{0,40}nicht", text, re.I):
        facts.hours_negotiable = True
        mark("hours_negotiable", match)
    if match := re.search(r"vollzeit|full[- ]time|temps plein", text, re.I):
        facts.full_time = True
        mark("full_time", match)
    if match := re.search(r"(?:keine|no|kein)\s+(?:teilzeit[^.;]*?)?werkstudent|keine\s+werkstudentenvertr", text, re.I):
        facts.no_student_contract = True
        mark("no_student", match)
    if not facts.no_student_contract and (match := re.search(
        r"werkstudent|working student|studentische|student assistant|stagiaire|praktikant|praktikum", text, re.I
    )):
        facts.student_contract = True
        mark("student", match)
    if match := re.search(r"\bsenior\b|\blead\b|\bprincipal\b", text, re.I):
        facts.senior = True
        mark("senior", match)
    if match := _YEARS.search(text):
        raw = match[1].lower()
        years = int(raw) if raw.isdigit() else _NUMBER_WORDS.get(raw)
        if years:
            facts.min_years_experience = years
            mark("years", match)
    if match := re.search(r"ausschlie(?:ß|ss)lich vor ort|100\s*%\s*präsenz|nur vor ort|on[- ]site only|fully on[- ]site|vollständig vor ort|kein homeoffice", text, re.I):
        facts.onsite_only = True
        mark("onsite_only", match)
    if match := re.search(r"hybrid|remote|homeoffice|home office|mobiles arbeiten|télétravail", text, re.I):
        if not facts.onsite_only:
            facts.hybrid_or_remote = True
            mark("hybrid", match)
    if match := _START.search(text):
        facts.start_date = parse_date(match[1].replace(".", " ").strip()) or _parse_dotted(match[1])
        mark("start", match)
    if match := re.search(r"(?:genauer\s+)?starttermin[^.;]{0,60}nicht|start date (?:is )?not", text, re.I):
        facts.start_unknown_stated = True
        mark("start_unknown", match)
    if match := re.search(
        r"mindestens (?:einem|einen|eine[rm]?) (?:der|dieser) (?:folgenden )?\w*|eine[rsnm]? der folgenden|"
        r"at least one of|one of the following|in (?:mindestens )?einem der (?:folgenden )?(?:bereiche|schwerpunkte)",
        text, re.I):
        facts.any_of_requirements = True
        mark("any_of", match)
    if match := re.search(r"immatrikul|eingeschrieben|enrolled", low):
        facts.enrollment_required = True
        facts.evidence["enrollment"] = _sentence(text, match.start(), match.end())
    return facts


def _parse_dotted(value: str) -> date | None:
    try:
        day, month, year = (int(p) for p in value.split("."))
        return date(year, month, day)
    except ValueError:
        return None


def parse_hours(value: str | None) -> tuple[int | None, int | None]:
    if not value:
        return None, None
    numbers = [int(n) for n in re.findall(r"\d{1,2}", value)]
    if not numbers:
        return None, None
    return min(numbers), max(numbers)
