"""Registry of the platform's AI capabilities: each is separate in code, configuration, API and tests.

No general chatbot spans these capabilities. ``tests/test_rag_capabilities.py`` checks that every listed endpoint
exists with the listed permission, and docs/ai-capabilities.md is generated from this registry.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Capability:
    key: str
    name: str
    input: str
    output: str
    permissions: tuple[str, ...]  # backend.security permissions; "worker" for background-only work
    endpoints: tuple[str, ...]  # "METHOD /path" (FastAPI path templates)
    configuration: str
    provider_dependency: str
    failure_behavior: str
    cache_policy: str
    evaluation: str
    modules: tuple[str, ...]


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        key="parsing",
        name="Resume parsing and OCR",
        input="Uploaded PDF/DOCX/TXT bytes (MAX_FILE_BYTES), registered under the caller's company prefix",
        output="Parsed text with hidden text removed, contact-redacted resume version (resumes.redacted_text) and "
        "provenance (source kind, pages, OCR pages, parser fingerprint)",
        permissions=("resumes:write", "worker"),
        endpoints=("POST /v1/resumes/register", "POST /v1/resumes/{resume_id}/reprocess"),
        configuration="MAX_FILE_BYTES, OCR_PROVIDER, TESSERACT_*, AZURE_DOCINTEL_*, OCR_MIN_CHARS",
        provider_dependency="Local PyMuPDF/python-docx; OCR by local Tesseract or Azure Document Intelligence",
        failure_behavior="Unreadable files become status failed; scans without OCR become needs_ocr. Nothing is "
        "screened or indexed until a reprocess produces a new processing generation.",
        cache_policy="None; ingestion is idempotent per content hash and processing generation",
        evaluation="tests/test_parsing.py (hidden text, reading order, OCR routing); layout/page tests",
        modules=("engine/parsing.py", "engine/local_ocr.py", "backend/services/resumes.py", "retrieval/layout.py"),
    ),
    Capability(
        key="fact_extraction",
        name="Structured candidate fact extraction",
        input="One resume version's redacted text",
        output="Verified CandidateExtraction (screening) and a versioned FactSet: located evidence, skill support "
        "levels, employment with overlaps and date warnings, accomplishments, availability, conflicts, review items",
        permissions=("rag:read",),
        endpoints=("GET /v1/rag/resumes/{resume_id}/facts", "POST /v1/rag/resumes/{resume_id}/facts/reviews"),
        configuration="EXTRACTION_MODE (screening); fact sets use deterministic rule extraction (FACTS_POLICY_VERSION)",
        provider_dependency="Screening: configured LLM or rules. Fact sets: none (rules), so indexing never waits "
        "for a model",
        failure_behavior="Unverifiable items are dropped and counted; a failed extraction yields a fact set with an "
        "extraction_failed review item, never guessed values",
        cache_policy="Company extraction cache keyed by text hash, mode, prompt, model/digest, ontology, verifier "
        "and code fingerprint; fact sets stored per (resume, processing generation, policy version)",
        evaluation="Field precision/recall, date accuracy, unsupported-fact rate, evidence validity "
        "(eval/rag/run_extraction_eval.py, eval/run_eval.py)",
        modules=("engine/extraction.py", "retrieval/facts.py", "backend/services/rag/indexing.py"),
    ),
    Capability(
        key="job_requirements",
        name="Job requirement extraction for human confirmation",
        input="Job description text",
        output="An unconfirmed requirements version; screening uses only versions a person confirmed",
        permissions=("jobs:write",),
        endpoints=("POST /v1/jobs/{job_id}/analyze", "POST /v1/jobs/{job_id}/requirements/confirm"),
        configuration="EXTRACTION_MODE, prompt jd_extraction",
        provider_dependency="Configured LLM, or rules offline",
        failure_behavior="LLM failure after retries returns 502; nothing is confirmed automatically",
        cache_policy="None",
        evaluation="tests/test_extraction.py JD cases; golden-set requirement extraction",
        modules=("engine/extraction.py", "backend/api/routes_jobs.py"),
    ),
    Capability(
        key="screening",
        name="Deterministic screening and scoring",
        input="Confirmed requirements version + frozen resume inputs (screening_inputs)",
        output="Score 0-100, knockouts, features with evidence, explanation; a recommendation for human review only",
        permissions=("screening:run", "candidates:read"),
        endpoints=("POST /v1/jobs/{job_id}/screening-runs", "GET /v1/jobs/{job_id}/candidates"),
        configuration="SCORE_WEIGHTS, KNOCKOUT_*, SKILL_RECENCY_*, EXPLANATION_MODE; frozen per run in its snapshot",
        provider_dependency="LLM only for extraction/explanations; scores are computed by code",
        failure_behavior="needs_reprocess results; retry runs reuse the frozen snapshot; incompatible workers refuse",
        cache_policy="Extraction checkpoints per run input; company extraction cache",
        evaluation="eval/run_eval.py: NDCG@10, precision@5, recall, fairness counterfactuals (CI gate)",
        modules=("engine/", "backend/services/screening.py", "backend/snapshots.py"),
    ),
    Capability(
        key="retrieval",
        name="Semantic candidate retrieval",
        input="Query text and/or structured skill filters; optional job scope; tenant from the access token",
        output="Candidates with resume version, evidence passages, matched filters, missing or uncertain evidence, "
        "retrieval signals and an explanation; relevance is never a screening score",
        permissions=("rag:read",),
        endpoints=("POST /v1/rag/search", "GET /v1/rag/evidence/{evidence_id}"),
        configuration="RAG_* search, fusion, vector backend and budget settings",
        provider_dependency="Embedding model optional: without it (or during an outage) results are keyword and "
        "filter based and marked semantic=unavailable",
        failure_behavior="Consent, tenant and version filters are applied in SQL before results exist; an unavailable "
        "vector backend degrades to keyword retrieval explicitly and never affects screening",
        cache_policy="Query embeddings in process memory keyed by company, space and query hash; no persistence",
        evaluation="Recall@k, precision@k, nDCG@10, MRR on held-out labelled queries; tenant/consent/version "
        "eligibility must be exact (eval/rag/run_retrieval_eval.py)",
        modules=("retrieval/query.py", "retrieval/fusion.py", "backend/services/rag/search.py"),
    ),
    Capability(
        key="evidence_qa",
        name="Evidence-backed question answering",
        input="Candidate, question (+ optional job for requirement coverage)",
        output="Validated statements with citations or the abstention 'Not found in the available resume "
        "evidence.', extractive evidence, skill/requirement coverage, extraction warnings, screening score separately",
        permissions=("rag:read",),
        endpoints=("POST /v1/rag/candidates/{candidate_id}/ask",),
        configuration="RAG_GENERATION, RAG_ASK_* budgets, ANSWER_PROMPT_VERSION",
        provider_dependency="Chat model optional: extractive answers without it; generation failures fall back to "
        "extractive evidence, never to unvalidated text",
        failure_behavior="Unsupported statements are rejected; eligibility is re-checked after generation",
        cache_policy="Answers cached in process memory only (TTL), keyed by tenant, role, document and index "
        "versions, model, prompt and retrieval config; eligibility re-checked on every hit",
        evaluation="Claim support, citation accuracy, abstention, contradiction handling, prompt-injection "
        "resistance (eval/rag/run_answer_eval.py)",
        modules=("retrieval/answers.py", "retrieval/prompts.py", "backend/services/rag/answers.py"),
    ),
    Capability(
        key="comparison",
        name="Candidate comparison and recruiter summaries",
        input="Job, one confirmed requirements version, 2-5 candidates",
        output="Per-requirement evidence coverage per candidate from the same version, deterministic screening "
        "results from that version shown separately, optional validated narrative",
        permissions=("rag:read",),
        endpoints=("POST /v1/rag/jobs/{job_id}/compare", "POST /v1/rag/jobs/{job_id}/evidence"),
        configuration="RAG_COMPARE_* budgets, RAG_GENERATION",
        provider_dependency="Chat model optional (narrative only)",
        failure_behavior="Unconfirmed versions are refused; candidates without eligible evidence are listed as "
        "excluded with a reason; narrative statements outside the requirement list are rejected",
        cache_policy="As evidence QA",
        evaluation="Coverage status agreement with labels; statement support; no criteria outside the version",
        modules=("backend/services/rag/answers.py",),
    ),
    Capability(
        key="model_evaluation",
        name="Operational model evaluation",
        input="Versioned synthetic datasets (eval/golden, eval/rag); authorized real sets only under eval/real",
        output="Metric reports and baselines with release gates; operations measurements",
        permissions=("worker",),
        endpoints=("GET /v1/rag/metrics",),
        configuration="FAIRNESS_SCOPE, RAG_EVAL_* (eval scripts)",
        provider_dependency="Optional local models for live runs; offline runs use deterministic stand-ins",
        failure_behavior="A regression beyond tolerance fails the gate; LLM judges are never the sole evidence",
        cache_policy="Evaluation embeddings cached under .cache (git-ignored)",
        evaluation="Held-out query splits; deterministic checks plus labelled examples",
        modules=("eval/",),
    ),
)
