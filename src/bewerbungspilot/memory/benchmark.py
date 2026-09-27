"""Build a memory snapshot from the anonymous benchmark profile.

``benchmarks/profiles/benchmark_profile.json`` is the test profile the expected benchmark
answers were written against. It contains no personal contact or legal data.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from bewerbungspilot.domain.enums import DisclosurePolicy as P, FactCategory as C, FactStatus, SourceRank
from bewerbungspilot.domain.models import CandidateFact

from .parsing import parse_date, slug
from .records import MemorySnapshot

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "benchmarks" / "profiles" / "benchmark_profile.json"
_VALIDATED = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)


def _fact(key: str, value, category: C, policy: P = P.APPLICATION_STANDARD, tags: list[str] | None = None) -> CandidateFact:
    return CandidateFact(
        key=key, value=value, category=category, status=FactStatus.CONFIRMED,
        source="benchmark_profile.json", source_rank=SourceRank.REFERENCE_PROFILE,
        source_ref="benchmark_profile", validated_at=_VALIDATED, disclosure_policy=policy, tags=tags or [],
    )


def memory_from_benchmark_profile(path: str | Path = DEFAULT_PATH) -> MemorySnapshot:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    facts = [
        _fact("availability.hours_per_week", int(data["weekly_hours"]), C.AVAILABILITY),
        _fact("availability.start_date", parse_date(data["availability"]).isoformat(), C.AVAILABILITY),
        _fact("preference.onsite_locations", list(data["regions_on_site"]), C.PREFERENCE),
        _fact("preference.remote_hybrid", "accepté" if data.get("hybrid_or_remote") else None, C.PREFERENCE)
        if data.get("hybrid_or_remote") else None,
        _fact("preference.job_type", "Werkstudent", C.PREFERENCE),
        _fact("education.enrollment_status", data["education"], C.EDUCATION),
        _fact("action_rule.forbidden_claims",
              [re.sub(r"\s+professionnel$", "", item) for item in data.get("not_confirmed_as_advanced", [])],
              C.ACTION_RULE, P.NEVER_EXPORT),
    ]
    for raw in data.get("skills_confirmed", []):
        name = re.sub(r"\s*\(.*\)$", "", raw).strip()
        level = "basics" if "base" in raw.lower() else "confirmed"
        facts.append(_fact(f"skill.{slug(name, 40)}", {"name": name, "level": level}, C.SKILL, P.PUBLIC_PROFILE,
                           [w for w in re.split(r"[^a-z0-9#+.]+", name.lower()) if w]))
    for language in data.get("languages", []):
        name, _, level = language.partition("(")
        facts.append(_fact(f"language.{slug(name.strip())}", level.rstrip(")").strip() or name, C.LANGUAGE))
    for name, description in data.get("projects", {}).items():
        words = {w for w in re.split(r"[^a-z0-9#+.]+", description.lower()) if len(w) >= 2}
        facts.append(_fact(f"project.{slug(name, 40)}", {"name": name, "points": [description]}, C.PROJECT,
                           P.PUBLIC_PROFILE, sorted(words)))
    return MemorySnapshot(facts=[f for f in facts if f is not None])
