"""Stable deduplication fingerprint for job offers.

The same position published on several platforms must produce the same fingerprint, and
the fingerprint of an offer must never change between runs or Python versions. Only
normalised employer, title and location enter the hash; the URL is canonicalised
separately because it differs between platforms.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

FINGERPRINT_VERSION = "v1"

_GENDER_MARKERS = re.compile(
    r"\(\s*(?:m|w|d|f|x|div|all genders?|alle geschlechter)"
    r"(?:\s*[/|,]\s*(?:m|w|d|f|x|div))*\s*\)",
    re.IGNORECASE,
)
_LEGAL_FORMS = re.compile(
    r"\b(gmbh\s*&\s*co\.?\s*kg|gmbh|ag|se|kg|kgaa|ug|e\.?\s?v\.?|mbh|inc\.?|ltd\.?|llc)\s*$",
    re.IGNORECASE,
)
_TRACKING_PARAMS = re.compile(r"^(utm_.*|ref|refid|source|src|trk|trackingid|campaign|gclid|fbclid)$", re.I)


def normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    text = text.replace("ß", "ss")
    text = re.sub(r"[‐-―]", "-", text)
    text = re.sub(r"\s+", " ", text).strip(" -–|,")
    return text


def normalize_title(title: str) -> str:
    text = _GENDER_MARKERS.sub(" ", unicodedata.normalize("NFKC", title))
    text = re.sub(r"\s*[-–|]\s*$", "", text)
    return normalize_text(text)


def normalize_employer(employer: str) -> str:
    text = normalize_text(employer)
    previous = None
    while previous != text:
        previous = text
        text = _LEGAL_FORMS.sub("", text).strip(" ,.")
    return text


def normalize_location(location: str) -> str:
    text = normalize_text(location)
    text = re.sub(r"\b\d{5}\b", "", text)  # German postcode
    text = re.sub(r"\s*,?\s*(deutschland|germany)$", "", text)
    return re.sub(r"\s+", " ", text).strip(" ,")


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False) if not _TRACKING_PARAMS.match(k)]
    path = re.sub(r"/+$", "", parts.path) or "/"
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, urlencode(sorted(query)), "")
    )


def offer_fingerprint(employer: str, title: str, location: str) -> str:
    """Lowercase SHA-256 accepted by ``JobOffer.deduplication_fingerprint``."""

    material = "|".join(
        (FINGERPRINT_VERSION, normalize_employer(employer), normalize_title(title), normalize_location(location))
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
