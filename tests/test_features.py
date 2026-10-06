from __future__ import annotations

import pytest

from engine.config import ConfigError, get_settings
from engine.features import (
    FEATURE_NAMES,
    build_context,
    candidate_profile_text,
    compute_features,
    domain_match,
    experience_score,
    job_profile_text,
    preferred_skill_coverage,
    project_relevance,
    relevant_experience,
    required_skill_coverage,
    semantic_relevance,
    seniority_fit,
    skill_recency_weight,
)
from engine.llm import EmbeddingResult
from engine.normalize import YearMonth, normalize_candidate
from engine.schemas import (
    CandidateExtraction,
    CandidateSkill,
    JobRequirements,
    LLMUsage,
    NormalizedCandidate,
    NormalizedJob,
    ProjectItem,
    RequirementSkill,
)
from engine.semantic import EmbeddingScorer, TfidfScorer, build_reranker, build_semantic_scorer

REF = YearMonth(2026, 10)


class FixedScorer:
    """SemanticScorer stub: similarity looked up by document text (default 0)."""

    def __init__(self, sims: dict[str, float] | None = None, name: str = "fixed") -> None:
        self.sims = sims or {}
        self.name = name
        self.calls: list[tuple[str, list[str]]] = []

    def similarities(self, query: str, docs: list[str]) -> list[float]:
        self.calls.append((query, docs))
        return [next((v for k, v in self.sims.items() if k in d), 0.0) for d in docs]


def _req(**overrides) -> JobRequirements:
    base = dict(
        role="Senior Machine Learning Engineer",
        seniority="senior",
        experience_min_years=5,
        required_skills=[
            RequirementSkill(name="Python", evidence="Strong Python"),
            RequirementSkill(name="PyTorch", evidence="Hands-on PyTorch"),
            RequirementSkill(name="Azure", evidence="Azure or AWS", alternatives=["AWS"]),
        ],
        preferred_skills=[RequirementSkill(name="LangChain", evidence="LangChain")],
        locations=[],
        max_notice_period_days=None,
    )
    base.update(overrides)
    return JobRequirements(**base)


def _skill(name: str, last_used: str | None = "2026-10") -> CandidateSkill:
    return CandidateSkill(canonical=name, raw=name, evidence=f"{name} evidence", in_ontology=True, last_used=last_used)


def _job(title: str, start: str, end: str, months: int, desc: str = "", level: int = 3) -> NormalizedJob:
    return NormalizedJob(
        title=title, company=None, start=start, end=end, is_current=False, months=months,
        date_confidence="high", seniority=level, description=desc, evidence=f"{title} {start}",
    )  # fmt: skip


def _cand(**overrides) -> NormalizedCandidate:
    base = dict(
        skills=[_skill("Python"), _skill("PyTorch"), _skill("AWS")],
        jobs=[],
        projects=[],
        total_experience_months=0,
        undated_jobs=0,
        seniority=4,
        location=None,
        notice_period_days=None,
        reference_month="2026-10",
    )
    base.update(overrides)
    return NormalizedCandidate(**base)


def _ctx(req=None, cand=None, semantic=None, reranker=None):
    return build_context(req or _req(), cand or _cand(), semantic or FixedScorer(), reranker=reranker)


# ---------------------------------------------------------------------------
# Skill coverage + recency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("last_used", "weight"),
    [
        ("2026-10", 1.0),
        ("2023-10", 1.0),  # exactly 36 months: still full weight
        ("2020-04", 0.8),  # 78 months: halfway between 3 and 10 years
        ("2016-10", 0.6),  # 10 years: floor
        ("2005-01", 0.6),
        (None, 0.5),  # listed in a skills section, no dated role
    ],
)
def test_skill_recency_weight(last_used: str | None, weight: float) -> None:
    assert skill_recency_weight(_skill("X", last_used), REF, get_settings()) == pytest.approx(weight)


def test_used_skills_always_outweigh_listed_only() -> None:
    s = get_settings()
    oldest = skill_recency_weight(_skill("X", "1990-01"), REF, s)
    assert oldest > skill_recency_weight(_skill("X", None), REF, s)


@pytest.mark.parametrize(
    "env",
    [
        {"SKILL_RECENCY_MIN_WEIGHT": "0.5", "SKILL_UNKNOWN_RECENCY_WEIGHT": "0.5"},  # equal is not enough
        {"SKILL_UNKNOWN_RECENCY_WEIGHT": "0.7"},
        {"SKILL_RECENCY_FLOOR_MONTHS": "30"},  # floor before the full-weight window ends
    ],
)
def test_recency_config_validator(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    with pytest.raises(ValueError):
        get_settings()


def test_required_skill_coverage_with_alternatives_and_recency() -> None:
    cand = _cand(skills=[_skill("Python"), _skill("PyTorch", "2020-04"), _skill("AWS", None)])
    f = required_skill_coverage(_ctx(cand=cand))
    assert f.applicable
    assert f.value == pytest.approx((1.0 + 0.8 + 0.5) / 3, abs=1e-4)
    assert "3/3 evidenced" in f.detail
    assert f.evidence == ["Python evidence", "PyTorch evidence", "AWS evidence"]


def test_required_skill_coverage_missing_skill() -> None:
    f = required_skill_coverage(_ctx(cand=_cand(skills=[_skill("Python")])))
    assert f.value == pytest.approx(1 / 3, abs=1e-4)
    assert "Missing: PyTorch, Azure or AWS" in f.detail


def test_skill_coverage_not_applicable_without_skills() -> None:
    req = _req(required_skills=[], preferred_skills=[])
    required = required_skill_coverage(_ctx(req=req))
    preferred = preferred_skill_coverage(_ctx(req=req))
    assert not required.applicable and not preferred.applicable
    assert "not applicable" in preferred.detail


# ---------------------------------------------------------------------------
# Relevant experience
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("months", "min_years", "score"),
    [
        (0, 5, 0.0),
        (30, 5, 0.4),  # r = 0.5 -> 0.8 * 0.5
        (60, 5, 0.8),  # r = 1.0
        (75, 5, 0.9),  # r = 1.25 -> 0.8 + 0.2 * 0.5
        (90, 5, 1.0),  # r = 1.5
        (200, 5, 1.0),  # capped
        (12, 0, 1.0),  # no minimum: any relevant experience
        (0, 0, 0.5),  # no minimum, no relevant experience
        (0, None, 0.5),
    ],
)
def test_experience_score_curve(months: int, min_years: float | None, score: float) -> None:
    assert experience_score(months, min_years) == pytest.approx(score)


def test_relevant_experience_exact_minimum() -> None:
    cand = _cand(jobs=[_job("Machine Learning Engineer", "2021-11", "2026-10", 60)])
    assert relevant_experience(_ctx(cand=cand)).value == pytest.approx(0.8)


def test_irrelevant_jobs_are_not_counted() -> None:
    cand = _cand(
        jobs=[
            _job("Machine Learning Engineer", "2024-11", "2026-10", 24),
            _job("Web Developer", "2015-01", "2024-10", 118, desc="React and Node.js"),
        ]
    )
    f = relevant_experience(_ctx(cand=cand))
    assert f.value == pytest.approx(0.8 * 24 / 60, abs=1e-4)
    assert "Web Developer" not in f.detail


def test_job_relevant_through_skill_overlap_despite_different_title() -> None:
    cand = _cand(jobs=[_job("Platform Engineer", "2021-11", "2026-10", 60, desc="Python, PyTorch and AWS models")])
    assert relevant_experience(_ctx(cand=cand)).value == pytest.approx(0.8, abs=1e-4)


def test_job_relevant_through_experience_domain() -> None:
    req = _req(experience_domain="machine learning or data science", required_skills=[], preferred_skills=[])
    cand = _cand(jobs=[_job("Lead Data Scientist", "2021-11", "2026-10", 60, desc="Clinical models")])
    f = relevant_experience(_ctx(req=req, cand=cand))
    assert f.value == pytest.approx(0.8) and "in machine learning or data science" in f.detail


@pytest.mark.parametrize(
    ("domain", "text", "ok"),
    [
        ("machine learning or data science", "Lead Data Scientist", True),
        ("machine learning or data science", "ML Consultant", True),  # ml -> machine learning
        ("machine learning or data science", "Data Analyst, SQL reporting", False),  # "data" alone isn't enough
        ("distributed systems", "Built distributed storage systems", True),
        (None, "Machine Learning Engineer", False),
    ],
)
def test_domain_match(domain: str | None, text: str, ok: bool) -> None:
    assert domain_match(domain, text) is ok


def test_overlapping_relevant_jobs_counted_once() -> None:
    cand = _cand(
        jobs=[
            _job("Machine Learning Engineer", "2022-01", "2023-12", 24),
            _job("ML Consultant", "2023-01", "2024-12", 24),  # 12 months overlap
        ]
    )
    assert "36 relevant months" in relevant_experience(_ctx(cand=cand)).detail


def test_relevant_experience_without_minimum() -> None:
    cand = _cand(jobs=[_job("Machine Learning Engineer", "2025-11", "2026-10", 12)])
    assert relevant_experience(_ctx(req=_req(experience_min_years=None), cand=cand)).value == 1.0
    assert relevant_experience(_ctx(req=_req(experience_min_years=0), cand=_cand())).value == 0.5


# ---------------------------------------------------------------------------
# Seniority
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("cand_level", "value"), [(4, 1.0), (5, 0.75), (2, 0.5), (1, 0.25)])
def test_seniority_fit(cand_level: int, value: float) -> None:
    assert seniority_fit(_ctx(cand=_cand(seniority=cand_level))).value == value


def test_seniority_fit_floor_at_zero() -> None:
    req = _req(seniority="principal")
    assert seniority_fit(_ctx(req=req, cand=_cand(seniority=1))).value == 0.0  # 1 - 0.25*5 < 0


def test_seniority_inferred_from_role_when_unset() -> None:
    req = _req(seniority=None, role="Staff ML Engineer")
    assert seniority_fit(_ctx(req=req, cand=_cand(seniority=5))).value == 1.0


# ---------------------------------------------------------------------------
# Projects & semantic relevance
# ---------------------------------------------------------------------------


def _project(name: str) -> ProjectItem:
    return ProjectItem(name=name, description=None, technologies=[], evidence=f"{name}: details")


def test_project_relevance_is_mean_of_top_three() -> None:
    projects = [_project(n) for n in ("Alpha", "Beta", "Gamma", "Delta")]
    scorer = FixedScorer({"Alpha": 0.9, "Beta": 0.6, "Gamma": 0.3, "Delta": 0.1})
    f = project_relevance(_ctx(cand=_cand(projects=projects), semantic=scorer))
    assert f.applicable and f.value == pytest.approx(0.6)
    assert f.evidence == ["Alpha: details", "Beta: details", "Gamma: details"]


def test_project_relevance_with_one_project() -> None:
    scorer = FixedScorer({"Alpha": 0.4})
    assert project_relevance(_ctx(cand=_cand(projects=[_project("Alpha")]), semantic=scorer)).value == 0.4


def test_project_relevance_falls_back_to_job_bullets() -> None:
    jobs = [
        _job("ML Engineer", "2020-01", "2026-10", 82, desc="Deployed PyTorch models\nBuilt RAG pipelines"),
        _job("Analyst", "2018-01", "2019-12", 24, desc="Excel reports"),
    ]
    scorer = FixedScorer({"PyTorch": 0.8, "RAG": 0.6, "Excel": 0.1})
    f = project_relevance(_ctx(cand=_cand(jobs=jobs), semantic=scorer))
    assert f.applicable and f.value == pytest.approx((0.8 + 0.6 + 0.1) / 3)
    assert "job bullet" in f.detail
    assert f.evidence == ["Deployed PyTorch models", "Built RAG pipelines", "Excel reports"]


def test_project_relevance_not_applicable_without_projects_or_bullets() -> None:
    f = project_relevance(_ctx(cand=_cand(jobs=[_job("ML Engineer", "2020-01", "2026-10", 82)])))
    assert not f.applicable and f.value == 0.0


def test_semantic_relevance_uses_reranker_when_enabled() -> None:
    base = FixedScorer({"Python": 0.2})
    reranker = FixedScorer({"Python": 0.9}, name="cross-encoder")
    f = semantic_relevance(_ctx(semantic=base, reranker=reranker))
    assert f.value == 0.9 and "cross-encoder" in f.detail
    assert base.calls == []


def test_candidate_profile_excludes_summary_and_unverified_text() -> None:
    cand = _cand(jobs=[_job("ML Engineer", "2020-01", "2026-10", 82, desc="PyTorch serving")])
    text = candidate_profile_text(cand)
    assert "ML Engineer" in text and "PyTorch serving" in text
    assert "summary" not in NormalizedCandidate.model_fields  # structurally impossible to use


def test_job_profile_text_uses_role_quotes_and_options() -> None:
    text = job_profile_text(_req())
    assert text.splitlines()[0] == "Senior Machine Learning Engineer"
    assert "Azure or AWS" in text and "Azure AWS" in text


def test_candidate_without_jobs_is_entry_level() -> None:
    extraction = CandidateExtraction(
        skills=[], jobs=[], projects=[], education=[], certifications=[],
        location=None, notice_period_days=None, summary=None,
    )  # fmt: skip
    cand = normalize_candidate(extraction)
    assert cand.seniority == 1
    f = seniority_fit(_ctx(cand=cand))  # job is senior (4): 1 - 0.25 * 3
    assert f.applicable and f.value == 0.25 and "entry level" in f.detail


def test_compute_features_returns_all_in_range() -> None:
    features = compute_features(_ctx(cand=_cand(jobs=[_job("ML Engineer", "2020-01", "2026-10", 82)])))
    assert [f.name for f in features] == list(FEATURE_NAMES)
    assert all(0.0 <= f.value <= 1.0 for f in features)


# ---------------------------------------------------------------------------
# Semantic backends
# ---------------------------------------------------------------------------


def test_tfidf_similarity_is_independent_of_other_candidates() -> None:
    jd = "Senior ML Engineer\nPython and PyTorch\nKubernetes deployment"
    scorer = TfidfScorer(jd)
    alone = scorer.similarities(jd, ["PyTorch models on k8s"])[0]
    batch = scorer.similarities(jd, ["React apps", "PyTorch models on k8s", "Accounting"])[1]
    assert alone == pytest.approx(batch) and alone > 0
    # Aliases map to canonical skill tokens: "k8s" matches the JD's "Kubernetes" (the literal word adds more).
    k8s, kubernetes, unrelated = scorer.similarities(jd, ["k8s", "Kubernetes", "Accounting"])
    assert 0 < k8s <= kubernetes and unrelated == 0


class FakeEmbedder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> EmbeddingResult:
        self.calls.append(texts)
        table = {"job": [1.0, 0.0], "same": [2.0, 0.0], "orthogonal": [0.0, 1.0], "opposite": [-1.0, 0.0]}
        return EmbeddingResult(
            vectors=[table[t] for t in texts],
            usage=LLMUsage(provider="fake", model="emb", prompt_tokens=len(texts), cost_usd=0.001),
        )


def test_embedding_scorer_cosine_clamped_and_cached() -> None:
    embedder = FakeEmbedder()
    scorer = EmbeddingScorer(embedder)
    assert scorer.similarities("job", ["same", "orthogonal", "opposite"]) == [1.0, 0.0, 0.0]
    scorer.similarities("job", ["same"])
    assert embedder.calls == [["job", "same", "orthogonal", "opposite"]]  # second call fully cached
    assert scorer.usage_tokens == 4


def test_semantic_backend_is_run_level(monkeypatch: pytest.MonkeyPatch) -> None:
    assert build_semantic_scorer("Python").name == "tfidf"  # no credentials => offline
    monkeypatch.setenv("SEMANTIC_BACKEND", "embeddings")
    get_settings.cache_clear()
    with pytest.raises(ConfigError):
        build_semantic_scorer("Python")


def test_reranker_flag_without_package_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    assert build_reranker() is None
    monkeypatch.setenv("SEMANTIC_RERANKER", "bge-reranker-v2-m3")
    get_settings.cache_clear()
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        with pytest.raises(ConfigError, match="sentence-transformers"):
            build_reranker()
    else:
        pytest.skip("sentence-transformers installed; flag-off/on behaviour covered elsewhere")
