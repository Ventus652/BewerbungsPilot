"""Single source of truth for secret detection and redaction.

Two different uses share this module:

* ``contains_forbidden_key`` — strict rejection used by business models: a form answer or
  an action log carrying a secret key is invalid;
* ``redact`` — best-effort scrubbing used by every journal and failure store before any
  byte is written to disk.
"""

from __future__ import annotations

import re
from typing import Any

FORBIDDEN_SECRET_KEYS = frozenset(
    {
        "password",
        "passwort",
        "otp",
        "one_time_code",
        "verification_code",
        "cookie",
        "session_cookie",
        "access_token",
        "refresh_token",
        "secret",
    }
)

# A key is redacted when one of its words (split on "_", "-", "." or spaces) is listed
# here, or when it contains one of the multi-word phrases below. Word matching avoids
# false positives such as ``prompt_tokens`` or ``footprint``.
_REDACT_KEY_WORDS = frozenset(
    {
        "password",
        "passwort",
        "passwd",
        "pwd",
        "token",
        "secret",
        "cookie",
        "cookies",
        "otp",
        "authorization",
        "apikey",
        "sessionid",
        "session",
        "personalausweis",
        "passport",
        "reisepass",
        "aufenthaltstitel",
        "iban",
        "pin",
    }
)
_REDACT_KEY_PHRASES = (
    "one_time",
    "verification_code",
    "api_key",
    "private_key",
    "id_card",
    "residence_permit",
    "titre_de_sejour",
    "credit_card",
)

REDACTED = "[REDACTED]"

_TEXT_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
    re.compile(r"(?i)\b(set-cookie|cookie)\s*:\s*[^\r\n]+"),
    re.compile(
        r"(?i)\b(password|passwort|passwd|pwd|token|access_token|refresh_token|api[_-]?key|"
        r"secret|otp|code|pin)\b\s*[:=]\s*[^\s,;\"']+"
    ),
    re.compile(r"\b(?:sk|ghp|gho|github_pat|xox[abp])[-_][A-Za-z0-9_]{16,}\b"),
    re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,3})?\b"),  # IBAN-like
)


def contains_forbidden_key(value: Any) -> str | None:
    """Return the first exact forbidden secret key found recursively, if any."""

    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).strip().lower()
            if normalized in FORBIDDEN_SECRET_KEYS:
                return normalized
            found = contains_forbidden_key(nested)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for nested in value:
            found = contains_forbidden_key(nested)
            if found:
                return found
    return None


def is_secret_key(key: object) -> bool:
    normalized = re.sub(r"[\s.\-]+", "_", str(key).strip().lower())
    if normalized in FORBIDDEN_SECRET_KEYS:
        return True
    if any(phrase in normalized for phrase in _REDACT_KEY_PHRASES):
        return True
    return any(word in _REDACT_KEY_WORDS for word in normalized.split("_") if word)


_CREDENTIAL_WORDS = frozenset(
    {"password", "passwort", "passwd", "pwd", "token", "secret", "cookie", "cookies", "otp", "apikey", "sessionid", "pin"}
)
_CREDENTIAL_PHRASES = ("one_time", "verification_code", "api_key", "private_key", "access_token", "refresh_token")


def is_credential_key(key: object) -> bool:
    """Narrower than ``is_secret_key``: authentication material only.

    The private memory may hold sensitive personal facts (e.g. a permit date) but never
    credentials; journals mask both.
    """

    normalized = re.sub(r"[\s.\-]+", "_", str(key).strip().lower())
    if normalized in FORBIDDEN_SECRET_KEYS or any(p in normalized for p in _CREDENTIAL_PHRASES):
        return True
    return any(word in _CREDENTIAL_WORDS for word in normalized.split("_") if word)


def redact_text(text: str) -> str:
    for pattern in _TEXT_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def redact(value: Any) -> Any:
    """Return a deep copy of ``value`` where secret keys and secret-looking strings are masked."""

    if isinstance(value, dict):
        return {
            key: (REDACTED if is_secret_key(key) else redact(nested))
            for key, nested in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(nested) for nested in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
