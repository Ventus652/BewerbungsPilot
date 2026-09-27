"""Portal adapter contract and a local fictional portal (phase 4.5).

Every real portal will implement the same four verbs — observe, fill/upload, read back,
submit — plus ``check_status`` to verify an uncertain submission without clicking again.
``FakePortal`` is an in-memory "DemoRecruit (TEST)" form used for end-to-end tests; it
counts submissions so a double submission is detectable.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, Field


class FieldKind(StrEnum):
    TEXT = "text"
    EMAIL = "email"
    PHONE = "phone"
    NUMBER = "number"
    DATE = "date"
    RADIO = "radio"
    SELECT = "select"
    FILE = "file"
    CHECKBOX = "checkbox"
    TEXTAREA = "textarea"


class FormField(BaseModel):
    id: str
    label: str
    kind: FieldKind
    required: bool = False
    options: list[str] = Field(default_factory=list)
    value: str | None = None


class FormSnapshot(BaseModel):
    portal: str
    title: str
    fields: list[FormField]


class SubmissionOutcome(BaseModel):
    confirmed: bool
    reference: str | None = None
    message: str


class PortalError(RuntimeError):
    """The portal failed; after a submit click the outcome must be treated as unknown."""


class PortalAdapter(Protocol):
    name: str

    def observe(self) -> FormSnapshot: ...
    def fill(self, field_id: str, value: str) -> None: ...
    def upload(self, field_id: str, path: Path) -> None: ...
    def read_back(self) -> dict[str, str | None]: ...
    def submit(self) -> SubmissionOutcome: ...
    def check_status(self) -> SubmissionOutcome: ...


DEMO_FIELDS = [
    FormField(id="vorname", label="Vorname", kind=FieldKind.TEXT, required=True),
    FormField(id="nachname", label="Nachname", kind=FieldKind.TEXT, required=True),
    FormField(id="email", label="E-Mail", kind=FieldKind.EMAIL, required=True),
    FormField(id="telefon", label="Telefonnummer", kind=FieldKind.PHONE, required=False),
    FormField(id="geschlecht", label="Geschlecht", kind=FieldKind.SELECT, options=["weiblich", "männlich", "divers"]),
    FormField(id="hochschule", label="Hochschule", kind=FieldKind.TEXT, required=True),
    FormField(id="wochenstunden", label="Wochenstunden", kind=FieldKind.NUMBER, required=True),
    FormField(id="eintritt", label="Frühester Eintritt", kind=FieldKind.DATE, required=True),
    FormField(id="abschluss", label="Voraussichtlicher Studienabschluss", kind=FieldKind.DATE),
    FormField(id="arbeitserlaubnis", label="Ist Ihre Arbeitserlaubnis für die gesamte Beschäftigungsdauer bis 2027 gültig?",
              kind=FieldKind.RADIO, required=True, options=["Ja", "Nein"]),
    FormField(id="lebenslauf", label="Lebenslauf hochladen", kind=FieldKind.FILE, required=True),
    FormField(id="anschreiben", label="Anschreiben hochladen", kind=FieldKind.FILE),
    FormField(id="datenschutz", label="Ich akzeptiere die Datenschutzerklärung", kind=FieldKind.CHECKBOX, required=True),
]


class FakePortal:
    """Local, deterministic, fictional portal. Never reachable from the internet."""

    name = "DemoRecruit (TEST)"

    def __init__(self, fields: list[FormField] | None = None, *, crash_on_submit: bool = False,
                 already_received: bool = False) -> None:
        self.fields = {f.id: f.model_copy() for f in (fields or DEMO_FIELDS)}
        self.values: dict[str, str | None] = {f: None for f in self.fields}
        self.submissions = 0
        self.crash_on_submit = crash_on_submit
        self.received_reference: str | None = f"DEMO-{uuid4().hex[:8].upper()}" if already_received else None

    def observe(self) -> FormSnapshot:
        return FormSnapshot(portal=self.name, title="DemoRecruit – Bewerbung (TEST)",
                            fields=[f.model_copy(update={"value": self.values[f.id]}) for f in self.fields.values()])

    def fill(self, field_id: str, value: str) -> None:
        field = self.fields[field_id]
        if field.kind in (FieldKind.RADIO, FieldKind.SELECT) and value not in field.options:
            raise PortalError(f"Option inconnue pour {field.label}: {value}")
        self.values[field_id] = value

    def upload(self, field_id: str, path: Path) -> None:
        if self.fields[field_id].kind != FieldKind.FILE or not Path(path).is_file():
            raise PortalError(f"Téléversement impossible : {path}")
        self.values[field_id] = Path(path).name

    def read_back(self) -> dict[str, str | None]:
        return dict(self.values)

    def submit(self) -> SubmissionOutcome:
        self.submissions += 1
        missing = [f.label for f in self.fields.values() if f.required and not self.values[f.id]]
        if missing:
            return SubmissionOutcome(confirmed=False, message="Pflichtfelder fehlen: " + ", ".join(missing))
        self.received_reference = f"DEMO-{uuid4().hex[:8].upper()}"
        if self.crash_on_submit:
            raise PortalError("Verbindung nach dem Klick auf Absenden unterbrochen")
        return SubmissionOutcome(confirmed=True, reference=self.received_reference,
                                 message="Vielen Dank! Ihre Bewerbung ist eingegangen.")

    def check_status(self) -> SubmissionOutcome:
        if self.received_reference:
            return SubmissionOutcome(confirmed=True, reference=self.received_reference,
                                     message="Status im Portal: Bewerbung eingegangen")
        return SubmissionOutcome(confirmed=False, message="Keine Bewerbung im Portal gefunden")
