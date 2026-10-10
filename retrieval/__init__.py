"""Evidence-backed retrieval for recruiting assistance.

This package is deliberately separate from ``engine/`` (screening). Screening scores, knockouts and frozen run
snapshots never read anything produced here, and nothing here changes engine code, so the screening
fingerprint (``backend.snapshots.engine_fingerprint``) is unaffected by retrieval changes.

Modules, by capability:

* ``layout``, ``chunking``: document preparation (structure, pages, noise, section-aware chunks).
* ``facts``: structured candidate facts with located evidence, warnings, conflicts and review items.
* ``query``, ``fusion``, ``rerank``: hybrid retrieval building blocks.
* ``answers``, ``prompts``: bounded context, structured answers, citation validation and abstention.
* ``embedding``: embedding-space identity, so vectors of different models are never compared.
* ``capabilities``: the capability registry (inputs, outputs, permissions, failure and cache policies).
"""

from retrieval.versions import (
    ANSWER_PROMPT_VERSION,
    CHUNKING_VERSION,
    FACTS_POLICY_VERSION,
    FACTS_SCHEMA_VERSION,
    LAYOUT_VERSION,
    RETRIEVAL_POLICY_VERSION,
)

__all__ = [
    "ANSWER_PROMPT_VERSION",
    "CHUNKING_VERSION",
    "FACTS_POLICY_VERSION",
    "FACTS_SCHEMA_VERSION",
    "LAYOUT_VERSION",
    "RETRIEVAL_POLICY_VERSION",
]
