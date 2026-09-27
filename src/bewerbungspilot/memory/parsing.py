"""Deterministic value parsers shared by the importer and the rules (French/German text)."""

from __future__ import annotations

import calendar
import re
import unicodedata
from datetime import date

MONTHS = {
    "janvier": 1, "fevrier": 2, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "aout": 8, "août": 8, "septembre": 9, "octobre": 10, "novembre": 11,
    "decembre": 12, "décembre": 12,
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "juni": 6, "juli": 7,
    "august": 8, "oktober": 10, "dezember": 12,
    "january": 1, "february": 2, "march": 3, "may": 5, "june": 6, "july": 7,
    "october": 10, "december": 12,
}

_MONTH_RE = "|".join(sorted((re.escape(m) for m in MONTHS), key=len, reverse=True))
_FULL_DATE = re.compile(rf"\b(\d{{1,2}})(?:er)?\.?\s+({_MONTH_RE})\s+(\d{{4}})\b", re.IGNORECASE)
_END_OF_MONTH = re.compile(rf"\b(?:fin|ende|end of)\s+({_MONTH_RE})\s+(\d{{4}})\b", re.IGNORECASE)
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def strip_markup(text: str) -> str:
    text = text.replace("**", "").replace("`", "")
    return re.sub(r"\s+", " ", text).strip().rstrip(".")


def slug(text: str, max_length: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return text[:max_length].strip("_") or "item"


def parse_date(text: str) -> date | None:
    """Absolute date only; ``fin septembre 2027`` means the last day of that month."""

    if match := _ISO.search(text):
        return date(int(match[1]), int(match[2]), int(match[3]))
    if match := _FULL_DATE.search(text):
        return date(int(match[3]), MONTHS[match[2].lower()], int(match[1]))
    if match := _END_OF_MONTH.search(text):
        year, month = int(match[2]), MONTHS[match[1].lower()]
        return date(year, month, calendar.monthrange(year, month)[1])
    return None


def parse_first_int(text: str) -> int | None:
    match = re.search(r"\b(\d{1,4})\b", text)
    return int(match[1]) if match else None


def parse_amount(text: str) -> float | None:
    match = re.search(r"(\d+(?:[.,]\d{1,2})?)\s*(?:€|eur)", text, re.IGNORECASE)
    return float(match[1].replace(",", ".")) if match else None


def parse_decimal(text: str) -> float | None:
    match = re.search(r"\b(\d+(?:[.,]\d+)?)\b", strip_markup(text))
    return float(match[1].replace(",", ".")) if match else None


def split_items(text: str) -> list[str]:
    """Split ``A, B et C`` / ``A, B or C`` into clean items."""

    parts = re.split(r",\s*|\s+(?:et|and|und|ou|or)\s+", strip_markup(text))
    return [p.strip() for p in parts if p.strip()]
