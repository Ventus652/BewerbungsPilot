"""Source priority and conflict detection (guide 3.5).

Rules:

* identical values: no conflict, the more authoritative source is kept as reference;
* different values, different authority: the more authoritative value is retained and a
  ``RESOLVED_BY_PRIORITY`` conflict stays visible — nothing is overwritten silently;
* different values, same authority (or no rank): an ``OPEN`` conflict is created, the key is
  marked ``needs_review`` and cannot be used automatically until the user decides;
* a model inference never confirms and never overrides anything.
"""

from __future__ import annotations

from dataclasses import dataclass

from bewerbungspilot.domain.enums import SOURCE_PRIORITY, SourceRank
from bewerbungspilot.domain.models import CandidateFact

from .records import ConflictStatus, ConflictValue, FactConflict

_LOWEST = len(SOURCE_PRIORITY)


def authority(fact: CandidateFact) -> int:
    """Smaller is stronger; unknown rank is weaker than any known source."""

    return SOURCE_PRIORITY.get(fact.source_rank, _LOWEST) if fact.source_rank else _LOWEST


def _normalize(value: object) -> object:
    if isinstance(value, str):
        return " ".join(value.casefold().split())
    if isinstance(value, list):
        return sorted(_normalize(v) for v in value)  # type: ignore[type-var]
    return value


def same_value(a: CandidateFact, b: CandidateFact) -> bool:
    return _normalize(a.model_dump(mode="json")["value"]) == _normalize(b.model_dump(mode="json")["value"])


@dataclass(frozen=True)
class MergeOutcome:
    retained: CandidateFact
    conflict: FactConflict | None
    changed: bool


def _conflict_value(fact: CandidateFact) -> ConflictValue:
    return ConflictValue(
        value=fact.model_dump(mode="json")["value"],
        source=fact.source or "unknown",
        source_ref=fact.source_ref,
        rank=fact.source_rank,
    )


def merge(existing: CandidateFact, incoming: CandidateFact) -> MergeOutcome:
    if existing.key != incoming.key:
        raise ValueError("merge() compares facts with the same key only")
    if incoming.source_rank == SourceRank.MODEL_INFERENCE:
        return MergeOutcome(existing, None, False)
    if same_value(existing, incoming):
        if authority(incoming) < authority(existing):
            return MergeOutcome(incoming, None, True)
        if authority(incoming) == authority(existing) and incoming.source_ref == existing.source_ref:
            # Re-import of the same source: refresh derived metadata (tags, comments), keep the identity.
            refreshed = incoming.model_copy(update={"id": existing.id, "needs_review": existing.needs_review})
            changed = refreshed.model_dump(exclude={"validated_at"}) != existing.model_dump(exclude={"validated_at"})
            return MergeOutcome(refreshed if changed else existing, None, changed)
        return MergeOutcome(existing, None, False)

    values = [_conflict_value(existing), _conflict_value(incoming)]
    a, b = authority(existing), authority(incoming)
    if a == b:
        retained = existing.model_copy(update={"needs_review": True})
        conflict = FactConflict(
            key=existing.key,
            values=values,
            status=ConflictStatus.OPEN,
            required_action="Choisir la valeur correcte ; la clé reste bloquée jusqu'à la décision de l'utilisateur.",
        )
        return MergeOutcome(retained, conflict, True)
    winner = existing if a < b else incoming
    conflict = FactConflict(
        key=existing.key,
        values=values,
        status=ConflictStatus.RESOLVED_BY_PRIORITY,
        retained_value=winner.model_dump(mode="json")["value"],
        required_action="Aucune action obligatoire ; vérifier que la source prioritaire est à jour.",
    )
    return MergeOutcome(winner, conflict, winner is incoming)
