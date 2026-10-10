"""Text helpers shared by layout, facts and answers: quote location and stable hashing."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from bisect import bisect_left

from engine.extraction import normalize_for_match

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_QUOTES = {
    **dict.fromkeys(map(ord, "‘’‚‛′"), "'"),
    **dict.fromkeys(map(ord, "“”„‟″"), '"'),
}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalized_with_offsets(text: str) -> tuple[str, list[int]]:
    """``normalize_for_match`` applied character by character, with each output character's source offset.

    Character-wise NFKC differs from whole-string NFKC only for decomposed sequences, which ``locate_quote``
    handles by falling back to an exact search.
    """
    chars: list[str] = []
    offsets: list[int] = []
    for i, ch in enumerate(text):
        for c in unicodedata.normalize("NFKC", ch).translate(_DASHES).translate(_QUOTES).casefold():
            if c.isspace():
                if not chars or chars[-1] == " ":
                    continue
                chars.append(" ")
            else:
                chars.append(c)
            offsets.append(i)
    while chars and chars[-1] == " ":
        chars.pop()
        offsets.pop()
    return "".join(chars), offsets


class QuoteLocator:
    """Find verified evidence quotes in one source text (built once per document)."""

    def __init__(self, text: str) -> None:
        self.text = text
        self._normalized, self._offsets = normalized_with_offsets(text)

    def locate(self, quote: str | None, *, start_at: int = 0) -> tuple[int, int] | None:
        """(start, end) of ``quote`` in the source, matched like the screening verifier; None if not found.

        ``start_at`` prefers occurrences at or after a source offset (used to resolve repeated lines in order).
        """
        if not quote or not quote.strip():
            return None
        needle = normalize_for_match(quote)
        if not needle:
            return None
        norm_start = bisect_left(self._offsets, start_at)
        index = self._normalized.find(needle, norm_start)
        if index < 0 and norm_start:
            index = self._normalized.find(needle)
        if index >= 0:
            return self._offsets[index], self._offsets[index + len(needle) - 1] + 1
        exact = self.text.find(quote.strip(), start_at)
        if exact < 0:
            exact = self.text.find(quote.strip())
        if exact >= 0:
            return exact, exact + len(quote.strip())
        return None


_WORD = re.compile(r"[a-z0-9][a-z0-9+#.\-]*", re.IGNORECASE)


def content_words(text: str, stopwords: frozenset[str]) -> list[str]:
    """Lower-cased word tokens without stopwords (light, deterministic; used for support checks)."""
    out = []
    for raw in _WORD.findall(text.casefold()):
        word = raw.strip(".-")
        if len(word) >= 2 and word not in stopwords:
            out.append(word)
    return out


def light_stem(word: str) -> str:
    """Crude suffix stripping so 'migrations'/'migrated'/'migrating' compare equal. Deterministic."""
    for suffix in ("ations", "ation", "ings", "ing", "ers", "ies", "ied", "ed", "es", "er", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word
