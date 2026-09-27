"""Technology names from offers → skill keys of the candidate memory."""

from __future__ import annotations

import re

from bewerbungspilot.memory.parsing import slug

# Offer spelling (slug) → memory skill slug.
ALIASES: dict[str, str] = {
    "rest": "api_rest",
    "rest_api": "api_rest",
    "rest_apis": "api_rest",
    "restful": "api_rest",
    "restful_apis": "api_rest",
    "api": "api_rest",
    "apis": "api_rest",
    "vertx": "vert_x",
    "vert_x": "vert_x",
    "sql_abfragen": "sql",
    "js": "javascript",
    "ts": "typescript",
    "react_js": "react",
    "reactjs": "react",
    "postgresql": "postgresql",
    "testing": "tests",  # generic word only; "UI-Tests"/"Unit-Tests" stay unconfirmed
    "pytorch": "pytorch",
    "ml": "machine_learning",
}

_PARENTHESIS = re.compile(r"\([^)]*\)")


def clean_technology(name: str) -> str:
    """``AWS (Expertise)`` → ``AWS``; keeps the visible technology name only."""

    return re.sub(r"\s+", " ", _PARENTHESIS.sub(" ", name)).strip(" -–:")


def split_technologies(name: str) -> list[str]:
    """``HTML/CSS`` → ``["HTML", "CSS"]``; ``C#/.NET`` stays whole."""

    cleaned = clean_technology(name)
    if cleaned.upper().replace(" ", "") in {"C#/.NET", ".NET/C#"}:
        return [cleaned]
    return [part.strip() for part in re.split(r"\s*/\s*", cleaned) if part.strip()]


def skill_slug(name: str) -> str:
    base = slug(clean_technology(name), 40)
    return ALIASES.get(base, base)


def requires_expertise(name: str) -> bool:
    return bool(re.search(r"expert|expertise|senior|fortgeschritten|advanced", name, re.IGNORECASE))


_INNER_LIST = re.compile(r"^(?P<label>[^()]*)\((?P<inner>[^()]*,[^()]*)\)\s*$")

# Words that name technologies (used to tell a missing skill from a business domain such as
# "ERP" or "Versand", which must never count as a missing competence).
TECH_VOCABULARY = frozenset(s.casefold() for s in (
    "Java", "Python", "JavaScript", "TypeScript", "React", "Angular", "Vue", "Vue.js", "Node.js", "Express", "Spring Boot",
    "Spring", "Kotlin", "C#", ".NET", "C#/.NET", "C++", "Go", "Rust", "PHP", "Symfony", "Laravel", "Ruby", "Rails", "Scala",
    "SQL", "MariaDB", "MySQL", "PostgreSQL", "Oracle", "MongoDB", "Redis", "Kafka", "Docker", "Kubernetes", "AWS", "Azure",
    "GCP", "Terraform", "Jenkins", "GitLab", "GitHub", "Git", "Linux", "Vert.x", "REST", "GraphQL", "MQTT", "HTML", "CSS",
    "Pandas", "NumPy", "PyTorch", "TensorFlow", "Jupyter", "KNIME", "Power BI", "Tableau", "SAP", "Swift", "UIKit",
    "SwiftUI", "Flutter", "Dart", "Selenium", "Cypress", "JUnit", "Figma", "Next.js", "Nuxt", "Svelte", "Tailwind",
    "Claude", "ChatGPT", "Copilot", "Amp", "LLM", "LLMs", "Matplotlib", "Godot", "GDScript", "ESP32", "Bash", "Shell",
    "Cloud", "ETL", "DevOps", "CI/CD", "API", "APIs", "iOS", "Android", "Microservices", "Spark", "Airflow", "dbt",
))


def expand_technology(item: str) -> list[str]:
    """``Frontend/Web (HTML, CSS, JavaScript)`` → ``[HTML, CSS, JavaScript]``; otherwise the visible names."""

    match = _INNER_LIST.match(item.strip())
    if match:
        return [part.strip() for part in re.split(r",\s*|\s+(?:und|and|oder|or)\s+", match["inner"]) if part.strip()]
    return split_technologies(item)


def looks_like_technology(name: str) -> bool:
    folded = name.casefold().strip()
    if folded in TECH_VOCABULARY or re.search(r"[.#+]|\d", folded):
        return True
    return any(part in TECH_VOCABULARY for part in re.split(r"[-/\s]+", folded) if part)  # e.g. "Cloud-ETL"


def normalize_groups(groups: list[list[str]]) -> list[list[str]]:
    """One track per item that lists its own technologies; plain items stay one track together."""

    tracks: list[list[str]] = []
    for group in groups:
        plain: list[str] = []
        for item in group:
            if _INNER_LIST.match(item.strip()):
                tracks.append(expand_technology(item))
            else:
                plain.extend(split_technologies(item))
        if plain:
            tracks.append(plain)
    return tracks
