"""Controlled import of the reference profile into candidate facts (guide 3.3).

Only reads files; never moves or modifies them. Every fact keeps its exact source
(file and line). Values stay in the private store; this module only knows the *labels*
of the reference profile and how to type their values.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Callable

from bewerbungspilot.domain.enums import (
    DisclosurePolicy as P,
    FactCategory as C,
    FactStatus,
    SourceRank,
)
from bewerbungspilot.domain.models import CandidateFact

from .parsing import (
    parse_amount,
    parse_date,
    parse_decimal,
    parse_first_int,
    slug,
    split_items,
    strip_markup,
)
from .records import SourceRecord, relative_display

PROFILE_FILE = "00_REFERENCE/PROFIL_ET_REGLES.md"

STOPWORDS = frozenset("""
a à au aux avec ce ces dans de des du en est et la le les lorsque mais ne ou où par pas peut plus pour que qui
se si son sur un une utiliser seulement pertinent diversité illustrer projets projet principal montrer placer
premier privilégier annonce annonces vise général générale tre être placé placée
der die das und oder mit für von zu im in ist ein eine auf als bei
the and or with for of to in on is an as at by
""".split())
_SHORT_TECH = frozenset({"ai", "ml", "ki", "ui", "ux", "go", "c#", "r"})


def keyword_tokens(text: str) -> set[str]:
    """Lower-case keywords (accents kept), without stop words or broken fragments."""

    tokens = set()
    for raw in re.split(r"[^\w#+.]+", text.lower()):
        token = raw.strip(".")
        if not token or token in STOPWORDS:
            continue
        if len(token) >= 3 or token in _SHORT_TECH:
            tokens.add(token)
    for part in re.split(r"[/]", text.lower()):
        for sub in re.split(r"[^\w#+.]+", part):
            if sub in _SHORT_TECH:
                tokens.add(sub)
    return tokens
GUIDE_FILE = "00_REFERENCE/MODE_EMPLOI_CANDIDATURES.md"


@dataclass(frozen=True)
class FieldSpec:
    key: str
    category: C
    policy: P
    parser: Callable[[str], Any] = strip_markup
    sensitive: bool = False
    tags: tuple[str, ...] = ()


def _bool_not_confirmed(text: str) -> bool:
    lowered = text.lower()
    return not any(neg in lowered for neg in ("pas encore", "non confirmé", "not yet", "noch nicht"))


def _locations(text: str) -> list[str]:
    items: list[str] = []
    for item in split_items(text):
        items.extend(part.strip() for part in item.split("/") if part.strip())
    return items


# (section slug, label prefix slug) -> spec. Labels come from the reference file structure.
PROFILE_FIELDS: dict[tuple[str, str], FieldSpec] = {
    ("identite_et_contact", "nom_utilise"): FieldSpec("identity.display_name", C.IDENTITY, P.APPLICATION_STANDARD),
    ("identite_et_contact", "genre"): FieldSpec("identity.form_gender", C.IDENTITY, P.FORM_REQUIRED_ONLY),
    ("identite_et_contact", "ville"): FieldSpec("contact.city", C.CONTACT, P.APPLICATION_STANDARD),
    ("identite_et_contact", "adresse_postale"): FieldSpec("contact.postal_address", C.CONTACT, P.ASK_BEFORE_USE),
    ("identite_et_contact", "e_mail"): FieldSpec("contact.email", C.CONTACT, P.APPLICATION_STANDARD),
    ("identite_et_contact", "telephone_affiche"): FieldSpec("contact.phone_international", C.CONTACT, P.APPLICATION_STANDARD),
    ("identite_et_contact", "telephone_au_format_national"): FieldSpec("contact.phone_national", C.CONTACT, P.FORM_REQUIRED_ONLY),
    ("identite_et_contact", "github"): FieldSpec("identity.github", C.IDENTITY, P.PUBLIC_PROFILE),
    ("etudes", "etablissement_actuel"): FieldSpec("education.institution", C.EDUCATION, P.PUBLIC_PROFILE),
    ("etudes", "formation_actuelle"): FieldSpec("education.program", C.EDUCATION, P.PUBLIC_PROFILE),
    ("etudes", "parcours_precedent"): FieldSpec("education.previous", C.EDUCATION, P.PUBLIC_PROFILE),
    ("etudes", "moyenne_universitaire"): FieldSpec("education.gpa", C.EDUCATION, P.FORM_REQUIRED_ONLY, parse_decimal),
    ("etudes", "releve_de_notes"): FieldSpec("education.transcript_summary", C.EDUCATION, P.FORM_REQUIRED_ONLY),
    ("etudes", "resultats_particulierement"): FieldSpec("education.relevant_grades", C.EDUCATION, P.APPLICATION_STANDARD, tags=("data", "ai", "database")),
    ("etudes", "fin_previsionnelle_du_bachelor"): FieldSpec("education.bachelor_expected_end", C.EDUCATION, P.APPLICATION_STANDARD, parse_date),
    ("etudes", "projet_d_etudes"): FieldSpec("education.master_intention", C.EDUCATION, P.APPLICATION_STANDARD),
    ("etudes", "statut"): FieldSpec("education.enrollment_status", C.EDUCATION, P.APPLICATION_STANDARD),
    ("etudes", "titre_de_sejour_actuel"): FieldSpec("legal.residence_permit_valid_until", C.LEGAL_SENSITIVE, P.FORM_REQUIRED_ONLY, parse_date, sensitive=True),
    ("etudes", "renouvellement"): FieldSpec("legal.residence_permit_renewal_confirmed", C.LEGAL_SENSITIVE, P.ASK_BEFORE_USE, _bool_not_confirmed, sensitive=True),
    ("etudes", "confidentialite"): FieldSpec("action_rule.residence_permit_confidentiality", C.ACTION_RULE, P.NEVER_EXPORT),
    ("disponibilite_et_conditions_recherchees", "date_de_disponibilite"): FieldSpec("availability.start_date", C.AVAILABILITY, P.APPLICATION_STANDARD, parse_date),
    ("disponibilite_et_conditions_recherchees", "temps_de_travail_souhaite"): FieldSpec("availability.hours_per_week", C.AVAILABILITY, P.APPLICATION_STANDARD, parse_first_int),
    ("disponibilite_et_conditions_recherchees", "pretention_salariale"): FieldSpec("compensation.hourly_gross_eur", C.COMPENSATION, P.FORM_REQUIRED_ONLY, parse_amount, tags=("werkstudent", "software")),
    ("disponibilite_et_conditions_recherchees", "type_de_poste_prioritaire"): FieldSpec("preference.job_type", C.PREFERENCE, P.APPLICATION_STANDARD),
    ("disponibilite_et_conditions_recherchees", "autres_formats_acceptables"): FieldSpec("preference.other_formats", C.PREFERENCE, P.NEVER_EXPORT),
    ("disponibilite_et_conditions_recherchees", "domaines_prioritaires"): FieldSpec("preference.domains", C.PREFERENCE, P.NEVER_EXPORT, split_items),
    ("disponibilite_et_conditions_recherchees", "presentiel_accepte"): FieldSpec("preference.onsite_locations", C.PREFERENCE, P.APPLICATION_STANDARD, _locations),
    ("disponibilite_et_conditions_recherchees", "teletravail_ou_modele_hybride"): FieldSpec("preference.remote_hybrid", C.PREFERENCE, P.APPLICATION_STANDARD),
    ("documents_de_reference_actuels", "cv_pdf_de_base"): FieldSpec("document.base_cv_path", C.SUPPORTING_DOCUMENT, P.NEVER_EXPORT),
    ("documents_de_reference_actuels", "generateur_du_cv"): FieldSpec("document.cv_generator_path", C.SUPPORTING_DOCUMENT, P.NEVER_EXPORT),
}

GUIDE_FIELDS: dict[tuple[str, str], FieldSpec] = {
    ("7_remplir_le_formulaire", "pour_l_universite"): PROFILE_FIELDS[("etudes", "etablissement_actuel")],
    ("7_remplir_le_formulaire", "pour_la_moyenne"): PROFILE_FIELDS[("etudes", "moyenne_universitaire")],
    ("7_remplir_le_formulaire", "adresse_postale"): PROFILE_FIELDS[("identite_et_contact", "adresse_postale")],
}

_BULLET = re.compile(r"^(\s*)(?:[-*]|\d+\.)\s+(.*)$")
_LABELLED = re.compile(r"^([^:]{2,120}?)\s*:\s+(.+)$")
_UPDATED = re.compile(r"Dernière mise à jour\s*:\s*(.+)$", re.IGNORECASE)


@dataclass
class ImportResult:
    facts: list[CandidateFact] = field(default_factory=list)
    sources: list[SourceRecord] = field(default_factory=list)
    applications: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validated_at(text: str, path: Path) -> datetime:
    for line in reversed(text.splitlines()):
        if match := _UPDATED.search(line):
            if parsed := parse_date(match[1]):
                return datetime.combine(parsed, time(12, 0), tzinfo=timezone.utc)
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


class _Builder:
    def __init__(self, source_id: str, display: str, rank: SourceRank, validated_at: datetime):
        self.source_id, self.display, self.rank, self.validated_at = source_id, display, rank, validated_at
        self.facts: dict[str, CandidateFact] = {}
        self.rule_counter: dict[str, int] = {}

    def add(self, spec: FieldSpec, raw: str, line: int, *, needs_review: bool = False) -> None:
        value = spec.parser(raw)
        if value is None or value == "" or value == []:
            return
        if isinstance(value, date):
            value = value.isoformat()
        self.facts[spec.key] = CandidateFact(
            key=spec.key,
            value=value,
            category=spec.category,
            status=FactStatus.CONFIRMED,
            source=f"{self.display}#L{line}",
            source_rank=self.rank,
            source_ref=f"{self.source_id}:L{line}",
            validated_at=self.validated_at,
            disclosure_policy=spec.policy,
            sensitive=spec.sensitive,
            needs_review=needs_review,
            tags=list(spec.tags),
            internal_comment=strip_markup(raw)[:2000] if spec.parser is not strip_markup else None,
        )

    def add_rule(self, section: str, text: str, line: int) -> None:
        index = self.rule_counter.get(section, 0) + 1
        self.rule_counter[section] = index
        key = f"action_rule.{section[:50]}.{index:02d}"
        self.facts[key] = CandidateFact(
            key=key,
            value=strip_markup(text),
            category=C.ACTION_RULE,
            status=FactStatus.CONFIRMED,
            source=f"{self.display}#L{line}",
            source_rank=self.rank,
            source_ref=f"{self.source_id}:L{line}",
            validated_at=self.validated_at,
            disclosure_policy=P.NEVER_EXPORT,
        )

    def add_list(self, key: str, category: C, policy: P, items: list[Any], line: int, tags: list[str] | None = None, *, sensitive: bool = False) -> None:
        self.facts[key] = CandidateFact(
            key=key,
            value=items,
            category=category,
            status=FactStatus.CONFIRMED,
            source=f"{self.display}#L{line}",
            source_rank=self.rank,
            source_ref=f"{self.source_id}:L{line}",
            validated_at=self.validated_at,
            disclosure_policy=policy,
            sensitive=sensitive,
            tags=tags or [],
        )


def _lookup(fields: dict[tuple[str, str], FieldSpec], section: str, label: str) -> FieldSpec | None:
    label_slug = slug(label)
    for (spec_section, prefix), spec in fields.items():
        if spec_section == section and label_slug.startswith(prefix):
            return spec
    return None


def _skill_items(text: str) -> list[dict[str, str]]:
    level = "confirmed"
    if match := _LABELLED.match(strip_markup(text)):
        head, qualifier = match[1], match[2]
        if "base" in qualifier.lower():
            level = "basics"
        text = head
    note = None
    if " dans le cadre de " in text:
        text, note = text.split(" dans le cadre de ", 1)
    items = []
    for name in split_items(text):
        entry = {"name": name, "level": level}
        if note:
            entry["context"] = f"dans le cadre de {note}"
        items.append(entry)
    return items


def parse_profile(path: Path, root: Path, *, rank: SourceRank = SourceRank.REFERENCE_PROFILE,
                  fields: dict[tuple[str, str], FieldSpec] | None = None, source_id: str = "profile") -> tuple[list[CandidateFact], SourceRecord]:
    text = path.read_text(encoding="utf-8")
    display = relative_display(path, root)
    builder = _Builder(source_id, display, rank, _validated_at(text, path))
    fields = PROFILE_FIELDS if fields is None else fields
    section = subsection = ""
    skills: list[dict[str, str]] = []
    skills_line = 0
    projects: dict[str, dict[str, Any]] = {}
    pending_list: tuple[FieldSpec, int, list[str]] | None = None

    def flush_list() -> None:
        nonlocal pending_list
        if pending_list:
            spec, line_no, items = pending_list
            if items:
                builder.add_list(spec.key, spec.category, spec.policy, items, line_no, list(spec.tags))
            pending_list = None

    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip()
        if line.startswith("## "):
            flush_list()
            section, subsection = slug(line[3:]), ""
            continue
        if line.startswith("### "):
            flush_list()
            subsection = line[4:].strip()
            if section == "projets_a_mettre_en_avant":
                projects[subsection] = {"name": subsection, "points": [], "line": number}
            continue
        if not line.strip() or line.startswith("#") or line.startswith(">") or line.startswith("```"):
            continue
        bullet = _BULLET.match(line)
        content = strip_markup(bullet[2]) if bullet else strip_markup(line)
        if _UPDATED.search(content):
            continue
        if bullet and len(bullet[1]) >= 2 and pending_list:
            pending_list[2].append(content)
            continue
        flush_list()
        if section == "projets_a_mettre_en_avant" and subsection in projects:
            projects[subsection]["points"].append(content)
            continue
        if section.startswith("competences") and bullet:
            skills.extend(_skill_items(bullet[2]))
            skills_line = skills_line or number
            continue
        if section == "langues" and bullet and (match := _LABELLED.match(content)):
            builder.add(FieldSpec(f"language.{slug(match[1])}", C.LANGUAGE, P.APPLICATION_STANDARD), match[2], number)
            continue
        if section.startswith("experience") and bullet and (match := _LABELLED.match(content)):
            builder.add(FieldSpec(f"experience.{slug(match[1])}", C.EXPERIENCE, P.APPLICATION_STANDARD), match[2], number)
            continue
        if section.startswith("competences") and content.lower().startswith("ne pas déclarer sans preuve"):
            claims = split_items(content.split(":", 1)[1])
            builder.add_list("action_rule.forbidden_claims", C.ACTION_RULE, P.NEVER_EXPORT, claims, number)
            continue
        match = _LABELLED.match(content) if bullet else None
        spec = _lookup(fields, section, match[1]) if match else None
        if spec is None and bullet and content.endswith(":"):
            spec = _lookup(fields, section, content[:-1])
            if spec is not None:
                pending_list = (spec, number, [])
                continue
        if spec is not None and match is not None:
            builder.add(spec, match[2], number)
        else:
            builder.add_rule(section or "general", content, number)
    flush_list()

    if skills:
        for skill in skills:
            key = f"skill.{slug(skill['name'], 40)}"
            builder.facts[key] = CandidateFact(
                key=key,
                value=skill,
                category=C.SKILL,
                status=FactStatus.CONFIRMED,
                source=f"{display}#L{skills_line}",
                source_rank=rank,
                source_ref=f"{source_id}:L{skills_line}",
                validated_at=builder.validated_at,
                disclosure_policy=P.PUBLIC_PROFILE,
                tags=[w for w in re.split(r"[^a-z0-9#+.]+", skill["name"].lower()) if w],
            )
    for name, project in projects.items():
        words = set()
        for point in project["points"]:
            words.update(keyword_tokens(point))
            for item in split_items(point):
                if len(item) <= 30 and len(keyword_tokens(item)) == 1:
                    words.update(keyword_tokens(item))
        builder.facts[f"project.{slug(name, 40)}"] = CandidateFact(
            key=f"project.{slug(name, 40)}",
            value={"name": name, "points": project["points"]},
            category=C.PROJECT,
            status=FactStatus.CONFIRMED,
            source=f"{display}#L{project['line']}",
            source_rank=rank,
            source_ref=f"{source_id}:L{project['line']}",
            validated_at=builder.validated_at,
            disclosure_policy=P.PUBLIC_PROFILE,
            tags=sorted(words),
        )
    source = SourceRecord(
        id=source_id,
        rank=rank,
        path=display,
        description="Profil de référence" if fields is PROFILE_FIELDS else "Mode d'emploi de référence",
        sha256=_sha256(path),
    )
    return list(builder.facts.values()), source


# --------------------------------------------------------------------------- old applications
_README_FIELD = re.compile(r"^-\s+([^:]+?)\s*:\s*(.+)$")
_OLD_OBSERVATIONS: tuple[tuple[re.Pattern[str], FieldSpec], ...] = (
    (re.compile(r"nom légal\s+([^;,.]+)", re.IGNORECASE), FieldSpec("identity.legal_name", C.IDENTITY, P.FORM_REQUIRED_ONLY)),
    (re.compile(r"fin prévisionnelle du Bachelor(?: le)?\s*:?\s*([^;.]+)", re.IGNORECASE), PROFILE_FIELDS[("etudes", "fin_previsionnelle_du_bachelor")]),
    (re.compile(r"Prétention salariale[^:]*:\s*([^;]+?€[^;.]*)", re.IGNORECASE), PROFILE_FIELDS[("disponibilite_et_conditions_recherchees", "pretention_salariale")]),
)
_INDEX_FIELDS = {
    "entreprise": "company",
    "poste": "position",
    "lieu": "location",
    "lien_officiel": "url",
    "date_de_candidature": "applied_on",
    "statut": "status",
}


def parse_old_application(folder: Path, root: Path) -> tuple[list[CandidateFact], SourceRecord, dict[str, Any]]:
    readme = folder / "README.md"
    text = readme.read_text(encoding="utf-8")
    display = relative_display(readme, root)
    source_id = f"old:{slug(folder.name, 100)}"
    validated = datetime.fromtimestamp(readme.stat().st_mtime, tz=timezone.utc)
    builder = _Builder(source_id, display, SourceRank.OLD_APPLICATION, validated)
    entry: dict[str, Any] = {"folder": folder.name, "source_id": source_id}
    for number, line in enumerate(text.splitlines(), start=1):
        if match := _README_FIELD.match(line.strip()):
            label = slug(match[1])
            if label in _INDEX_FIELDS and _INDEX_FIELDS[label] not in entry:
                entry[_INDEX_FIELDS[label]] = strip_markup(match[2])
        for pattern, spec in _OLD_OBSERVATIONS:
            if spec.key in builder.facts:
                continue
            if found := pattern.search(line):
                # An old dossier alone never makes a fact usable automatically.
                builder.add(spec, found[1], number, needs_review=True)
    status = entry.get("status", "").lower()
    entry["sent"] = any(word in status for word in ("envoy", "sent", "déjà"))
    source = SourceRecord(
        id=source_id,
        rank=SourceRank.OLD_APPLICATION,
        path=display,
        description=f"Ancien dossier de candidature {folder.name}",
        sha256=_sha256(readme),
    )
    return list(builder.facts.values()), source, entry


def list_supporting_documents(root: Path) -> list[SourceRecord]:
    """Official PDFs are registered (hash only); their content is not parsed automatically."""

    records: dict[str, SourceRecord] = {}
    for pdf in sorted(root.glob("*/*Leistungsuebersicht*.pdf")):
        digest = _sha256(pdf)
        if digest in records:
            continue
        records[digest] = SourceRecord(
            id=f"official:transcript:{digest[:12]}",
            rank=SourceRank.OFFICIAL_DOCUMENT,
            path=relative_display(pdf, root),
            description="Relevé de notes officiel (PDF)",
            sha256=digest,
            processed=False,
            note="Non extrait automatiquement : vérifier manuellement ECTS et moyenne contre le profil.",
        )
    return list(records.values())


def import_sources(root: Path) -> ImportResult:
    root = Path(root)
    result = ImportResult()
    profile = root / PROFILE_FILE
    guide = root / GUIDE_FILE
    if not profile.exists():
        raise FileNotFoundError(f"Reference profile not found: {PROFILE_FILE}")
    facts, source = parse_profile(profile, root)
    result.facts += facts
    result.sources.append(source)
    if guide.exists():
        facts, source = parse_profile(guide, root, fields=GUIDE_FIELDS, source_id="guide")
        # Only the labelled duplicates are facts; the guide's procedures stay action rules.
        result.facts += facts
        result.sources.append(source)
    else:
        result.notes.append(f"{GUIDE_FILE} absent")
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and (p / "README.md").exists() and p.name[:4].isdigit()):
        facts, source, entry = parse_old_application(folder, root)
        result.facts += facts
        result.sources.append(source)
        result.applications.append(entry)
    result.sources += list_supporting_documents(root)
    return result
