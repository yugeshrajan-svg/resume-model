"""Redaction applied to chunk text before it is stored, embedded or shown as retrieval evidence.

Ingestion already replaces the candidate's name, email addresses and phone numbers in ``redacted_text`` (see
``backend/contacts.py``); that text is the screening input and is not changed here. Retrieval adds a second,
independent pass for contact-like details that ingestion does not cover: profile URLs, social handles, residual
emails/phones (defence in depth) and street addresses. Contact details are available only through the
separately authorized contact endpoints.
"""

from __future__ import annotations

import re
from collections import Counter

URL_RE = re.compile(
    r"\b(?:https?://|www\.)[^\s<>()\[\]|,;]+"
    r"|\b(?:linkedin\.com|github\.com|gitlab\.com|bitbucket\.org|twitter\.com|x\.com|medium\.com|behance\.net"
    r"|dribbble\.com|kaggle\.com|stackoverflow\.com|leetcode\.com|hackerrank\.com)/[^\s<>()\[\]|,;]*",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
HANDLE_RE = re.compile(r"(?<![\w.@\[])@[A-Za-z0-9_]{2,30}\b")
PHONE_RE = re.compile(r"(?<![\w+])(?:\+\d{1,3}[\s.-]?)?(?:\(?\d{2,5}\)?[\s.-]?){2,4}\d{2,5}(?!\w)")
# Labelled addresses and postal codes are redacted anywhere. Street-like patterns ("12 Main Street") only in
# the contact header: in body text the same words are ordinary prose ("led 5 cross-functional teams").
LABELLED_ADDRESS_RE = re.compile(
    r"\b(?:address|residence|addr\.)\s*[:\-]\s*[^\n|]+"
    r"|\b(?:pin\s*code|pincode|postal\s+code|zip(?:\s*code)?)\s*[:\-]?\s*\d{5,6}\b",
    re.IGNORECASE,
)
STREET_RE = re.compile(
    r"\b\d{1,5}[A-Za-z]?(?:[/-]\d{1,4})?,?\s+(?:[A-Za-z.'-]+\s+){0,4}"
    r"(?:street|st\.|road|rd\.|avenue|ave\.|lane|ln\.|boulevard|blvd\.?|drive|dr\.|nagar|colony|sector|block"
    r"|apartments?|apt\.?|flat|floor|layout|cross|main\s+road|marg|society)\b[^\n|]*",
    re.IGNORECASE,
)
PLACEHOLDERS = ("[NAME]", "[EMAIL]", "[PHONE]", "[URL]", "[HANDLE]", "[ADDRESS]")


def _is_phone(raw: str) -> bool:
    return 10 <= len(re.sub(r"\D", "", raw)) <= 15


def redact_chunk_text(text: str, *, header: bool = False) -> tuple[str, Counter[str]]:
    """Text with contact-like details replaced by placeholders, and the number of replacements by kind.

    ``header``: the text comes from the resume's contact header, where street-like patterns are addresses.
    """
    counts: Counter[str] = Counter()

    def sub(pattern: re.Pattern[str], placeholder: str, kind: str, value: str, check=None) -> str:
        def repl(m: re.Match[str]) -> str:
            if check is not None and not check(m.group(0)):
                return m.group(0)
            counts[kind] += 1
            return placeholder

        return pattern.sub(repl, value)

    out = sub(URL_RE, "[URL]", "url", text)
    out = sub(EMAIL_RE, "[EMAIL]", "email", out)
    out = sub(HANDLE_RE, "[HANDLE]", "handle", out)
    out = sub(PHONE_RE, "[PHONE]", "phone", out, check=_is_phone)
    out = sub(LABELLED_ADDRESS_RE, "[ADDRESS]", "address", out)
    if header:
        out = sub(STREET_RE, "[ADDRESS]", "address", out)
    return out, counts


def only_placeholders(text: str) -> bool:
    """True if nothing but placeholders, labels and punctuation remains (e.g. a redacted contact header)."""
    rest = text
    for placeholder in PLACEHOLDERS:
        rest = rest.replace(placeholder, " ")
    rest = re.sub(r"\b(?:e-?mail|phone|mobile|tel|contact|linkedin|github|portfolio|website)\b", " ", rest, flags=re.I)
    return not re.search(r"[A-Za-z0-9]", rest)
