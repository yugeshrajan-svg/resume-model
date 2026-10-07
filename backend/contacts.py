"""Minimal contact extraction (name, email, phone), deduplication hashes, and redaction.

This is the ONLY code that reads contact details out of a resume. Its output goes to candidate_contacts for HR
display and deduplication; the screening path only ever receives ``redact(...)``-ed text, so names, emails and
phone numbers cannot reach extraction, scoring or explanations.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Phone numbers: optional +country code, 10+ digits with common separators. Year ranges ("2019 - 2021") have
# too few digits to match.
PHONE_RE = re.compile(r"(?<![\w+])(?:\+\d{1,3}[\s.-]?)?(?:\(?\d{2,5}\)?[\s.-]?){2,4}\d{2,5}(?!\w)")
_NAME_LINE = re.compile(r"^[A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.'-]*){1,3}$")
_NOT_A_NAME = re.compile(
    r"\b(resume|curriculum|vitae|cv|summary|profile|experience|skills|education|projects|contact|location|"
    r"engineer|developer|scientist|analyst|manager|intern|consultant|lead)\b",
    re.IGNORECASE,
)
REDACTED_NAME, REDACTED_EMAIL, REDACTED_PHONE = "[NAME]", "[EMAIL]", "[PHONE]"


@dataclass(frozen=True)
class Contacts:
    name: str | None
    email: str | None
    phone: str | None


def _phone_digits(raw: str) -> str:
    return re.sub(r"\D", "", raw)


def _is_phone(raw: str) -> bool:
    return 10 <= len(_phone_digits(raw)) <= 15


def extract_contacts(text: str) -> Contacts:
    email = next(iter(EMAIL_RE.findall(text)), None)
    phone = next((m.group(0).strip() for m in PHONE_RE.finditer(text) if _is_phone(m.group(0))), None)
    name = None
    for line in [ln.strip() for ln in text.splitlines() if ln.strip()][:6]:
        if _NAME_LINE.match(line) and not _NOT_A_NAME.search(line):
            name = line
            break
    return Contacts(name=name, email=email, phone=phone)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def normalize_phone(phone: str) -> str:
    """Digits only; Indian numbers reduced to their 10-digit national form (+91 / 0 prefixes removed)."""
    digits = _phone_digits(phone)
    if len(digits) == 12 and digits.startswith("91"):
        return digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        return digits[1:]
    return digits


def contact_hash(kind: str, value: str, key: bytes) -> str:
    """Keyed hash (HMAC-SHA256) of a normalized email/phone, used for deduplication lookups."""
    normalized = normalize_email(value) if kind == "email" else normalize_phone(value)
    return hmac.new(key, f"{kind}:{normalized}".encode(), hashlib.sha256).hexdigest()


def redact(text: str, contacts: Contacts) -> str:
    """Remove every email address, phone number and the candidate's name (and its parts) from ``text``."""
    out = EMAIL_RE.sub(REDACTED_EMAIL, text)
    out = PHONE_RE.sub(lambda m: REDACTED_PHONE if _is_phone(m.group(0)) else m.group(0), out)
    if contacts.name:
        out = re.sub(re.escape(contacts.name), REDACTED_NAME, out, flags=re.IGNORECASE)
        for part in contacts.name.split():
            if len(part) >= 3:
                out = re.sub(rf"(?<![\w]){re.escape(part)}(?![\w])", REDACTED_NAME, out, flags=re.IGNORECASE)
    return out
