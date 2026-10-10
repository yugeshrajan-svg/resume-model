"""Untrusted-content and output-policy checks for retrieval and answers.

Resume text is data, never instructions. Chunks that contain instruction-like text are flagged at indexing time
and the matching spans are neutralized before any model sees them; answer statements that make hiring
recommendations, evaluate character or mention protected characteristics are rejected after generation,
whatever the evidence says.
"""

from __future__ import annotations

import re

INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}\b(?:previous|prior|above|earlier|all|any|these)\b"
        r"[^.\n]{0,30}\b(?:instructions?|prompts?|directions?|rules?|guidelines?)\b",
        r"\b(?:system|developer|assistant)\s*(?:prompt|message|instructions?)\b",
        r"^\s*(?:system|assistant|user)\s*:",
        r"<\s*/?\s*(?:system|assistant|instructions?|prompt)\s*>",
        r"\b(?:BEGIN|END)\s+(?:SYSTEM|INSTRUCTIONS?|PROMPT)\b",
        r"\byou\s+are\s+(?:now\s+)?(?:an?|the)\s+(?:ai|assistant|model|chatbot|language\s+model|recruiter|screener)\b",
        r"\b(?:rank|rate|score|recommend|select|shortlist|hire|pass)\s+(?:this|the|me|him|her|them)\b[^.\n]{0,40}"
        r"\b(?:first|highest|top|best|highly|immediately|#\s?1|number\s+one)\b",
        r"\b(?:this|the)\s+(?:candidate|applicant|resume|cv)\s+(?:is|should\s+be)\s+(?:the\s+)?"
        r"(?:best|top|perfect|ideal|strongest|hired|shortlisted|selected)\b",
        r"\b(?:note|message|instruction)\s+(?:to|for)\s+(?:the\s+)?(?:ai|llm|model|assistant|chatgpt|gpt|recruiter\s+bot)\b",
    )
)
NEUTRALIZED = "[instruction-like text removed]"

# Hiring recommendations, rankings and character judgements: retrieval answers report evidence only.
EVALUATIVE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:should|must|would)\s+(?:be\s+)?(?:hire[d]?|reject(?:ed)?|shortlist(?:ed)?|interview(?:ed)?|"
        r"advance[d]?|select(?:ed)?)\b",
        r"\b(?:i|we)\s+(?:recommend|suggest|advise)\b",
        r"\brecommend(?:ed|s)?\s+(?:hiring|for\s+(?:hire|the\s+role)|this\s+candidate)\b",
        r"\b(?:best|ideal|perfect|top|strongest|weakest|ideal)\s+(?:candidate|fit|choice|applicant|hire)\b",
        r"\b(?:great|good|strong|poor|bad|excellent|weak)\s+(?:fit|match|candidate|hire)\b",
        r"\b(?:culture|cultural)\s+fit\b",
        # Character judgements predicated of the person ("reliable pipelines" or "data integrity" are fine).
        r"\b(?:candidate|applicant|they|person)\s+(?:is|are|seems?|appears?|was|were)\s+(?:very\s+|highly\s+|"
        r"quite\s+|clearly\s+)?(?:an?\s+)?(?:honest|dishonest|trustworthy|untrustworthy|lazy|hard-?working|"
        r"reliable|unreliable|passionate|motivated|self-motivated|likeable|arrogant|humble|team\s+player|"
        r"natural\s+leader|smart|intelligent|talented)\b",
        r"\b(?:personality|temperament|work\s+ethic|honesty|trustworthiness)\b",
        r"\b(?:more|less)\s+(?:qualified|suitable|experienced|skilled)\s+than\b",
        r"\b(?:ranks?|ranked|score[sd]?)\s+(?:higher|lower|first|highest|best)\b",
    )
)
# Protected or sensitive characteristics. A statement may use such a word only when the cited evidence contains
# it (an employer "Helix Health", a "medical imaging" project); otherwise the word is an inference and the
# statement is rejected. Protected attribute lines themselves never reach evidence (see layout).
PROTECTED_TERMS = re.compile(
    r"\b(?:age[ds]?|years?\s+old|young(?:er)?|elderly|gender|male|female|man|woman|men|women|"
    r"pregnan\w*|maternity|paternity|married|marital|single\s+parent|divorced|widow\w*|"
    r"religio\w*|caste|ethnic\w*|race|racial|nationality|citizenship|national\s+origin|disab\w*|health|"
    r"medical|illness|sexual|orientation|gay|lesbian|transgender|veteran|family|children|kids|"
    r"date\s+of\s+birth|birth|accent|native\s+speaker)\b",
    re.IGNORECASE,
)
# Answers refer to "the candidate"; gendered pronouns would imply an inferred gender.
GENDERED_PRONOUNS = re.compile(r"\b(?:he|she|his|her|him|hers|himself|herself)\b", re.IGNORECASE)


def injection_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for pattern in INJECTION_PATTERNS:
        for m in pattern.finditer(text):
            spans.append((m.start(), m.end()))
    return sorted(spans)


def has_injection(text: str) -> bool:
    return any(pattern.search(text) for pattern in INJECTION_PATTERNS)


def neutralize(text: str) -> str:
    """Replace instruction-like spans (and the rest of their sentence) before text is sent to a model."""
    spans = injection_spans(text)
    if not spans:
        return text
    out, cursor = [], 0
    for start, end in spans:
        if start < cursor:
            continue
        # Extend to the end of the sentence or line: the instruction's payload usually follows the trigger.
        stop = re.search(r"[.!?\n]", text[end:])
        end = end + (stop.end() if stop else len(text) - end)
        out.append(text[cursor:start])
        out.append(NEUTRALIZED)
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def evaluative(text: str) -> bool:
    return any(pattern.search(text) for pattern in EVALUATIVE_PATTERNS)


def protected_terms(text: str) -> set[str]:
    return {" ".join(m.group(0).casefold().split()) for m in PROTECTED_TERMS.finditer(text)}


def ungrounded_protected_terms(statement: str, evidence: str) -> set[str]:
    """Protected terms in ``statement`` that its cited evidence does not contain."""
    return protected_terms(statement) - protected_terms(evidence)


def gendered(text: str) -> bool:
    return GENDERED_PRONOUNS.search(text) is not None
