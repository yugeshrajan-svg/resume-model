"""Talent pool against PostgreSQL: vectors on ingestion, semantic search, add-to-job, privacy and isolation.

A deterministic fake embedding model stands in for the local model, so the test runs offline.
"""

from __future__ import annotations

import asyncio
import math
import uuid

import pytest
from sqlalchemy import delete, select

from backend.db import NotFound
from backend.models import CandidateEmbedding, QueueJob, UsageEvent
from backend.queue import ClaimedJob
from backend.services import talent_pool
from backend.worker import Worker
from engine.config import get_settings
from engine.llm import EmbeddingResult, LLMError
from engine.schemas import LLMUsage
from tests.test_backend_integration import platform, register
from tests.test_backend_integration import settings as settings
from tests.test_durable_workflow import item, make_job, upload
from tests.test_hr_management import add_user, auth

pytestmark = pytest.mark.integration
VOCABULARY = ("python", "pytorch", "kubernetes", "machine learning", "sql", "accounting", "payroll", "excel", "tax")
ML_RESUME = (
    b"Asha Iyer\nasha.iyer@example.com\n\nExperience\nMachine Learning Engineer | Nimbus | Jan 2020 - Present\n"
    b"- Built machine learning models with Python and PyTorch on Kubernetes\n\nSkills\nPython, PyTorch, SQL\n"
)
ACCOUNTANT_RESUME = (
    b"Ravi Menon\nravi.menon@example.com\n\nExperience\nSenior Accountant | Ledger Co | Mar 2016 - Present\n"
    b"- Payroll, tax filings and month-end accounting in Excel\n\nSkills\nAccounting, Payroll, Excel, Tax\n"
)
ACCOUNTANT_JD = "Senior accountant. Requirements: payroll, tax and accounting experience; advanced Excel."


class FakeEmbedder:
    """Keyword-count vectors: documents about the same topics point the same way."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def _vector(self, text: str) -> list[float]:
        low = text.lower()
        return [float(low.count(word)) + 0.01 for word in VOCABULARY]

    def embed(self, texts: list[str]) -> EmbeddingResult:
        if self.fail:
            raise LLMError("model server unavailable")
        usage = LLMUsage(provider="ollama", model="fake-embed", prompt_tokens=10 * len(texts), cost_usd=0.0)
        return EmbeddingResult(vectors=[self._vector(t) for t in texts], usage=usage)

    embed_query = embed


@pytest.fixture
def embeddings(monkeypatch: pytest.MonkeyPatch) -> FakeEmbedder:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("SEMANTIC_BACKEND", "embeddings")
    get_settings.cache_clear()
    fake = FakeEmbedder()
    monkeypatch.setattr(talent_pool, "get_embedder", lambda _settings: fake)
    return fake


async def drain(svc, limit: int = 30) -> None:
    for _ in range(limit):
        if not await Worker(svc).run_once():
            return


async def ingest(svc, client, job_id: str, name: str, data: bytes) -> str:
    target = await upload(svc, client, name, data)
    r = await client.post("/v1/resumes/register", json=item(target, job_id))
    assert r.status_code == 201, r.text
    return r.json()["resume"]["id"]


async def candidate_of(client, resume_id: str) -> str:
    return (await client.get(f"/v1/resumes/{resume_id}")).json()["candidate_id"]


def test_ingestion_indexes_resumes_and_search_ranks_by_meaning(settings, embeddings) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            admin = await register(client, companies)
            ml_job = await make_job(client)
            ml_resume = await ingest(svc, client, ml_job, "asha.txt", ML_RESUME)
            acc_resume = await ingest(svc, client, ml_job, "ravi.txt", ACCOUNTANT_RESUME)
            await drain(svc)
            ml, acc = await candidate_of(client, ml_resume), await candidate_of(client, acc_resume)
            company = uuid.UUID(admin["company_id"])
            async with svc.db.system_session() as s:
                rows = (
                    (await s.execute(select(CandidateEmbedding).where(CandidateEmbedding.company_id == company)))
                    .scalars()
                    .all()
                )
                assert {str(r.candidate_id) for r in rows} == {ml, acc}
                assert {r.model for r in rows} == {"ollama:nomic-embed-text:v1.5"}
                assert all(r.dimension == len(VOCABULARY) for r in rows)
                assert all(math.isclose(math.hypot(*r.vector), 1.0, rel_tol=1e-4) for r in rows)  # stored unit-length
                kinds = (await s.execute(select(UsageEvent.kind).where(UsageEvent.company_id == company))).scalars()
                assert "embedding" in set(kinds)

            r = await client.post("/v1/talent-pool/search", json={"query": "python pytorch machine learning"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["indexed_candidates"] == 2 and body["resumes_pending_index"] == 0
            assert [x["candidate_id"] for x in body["results"]] == [ml, acc]
            assert body["results"][0]["similarity"] > body["results"][1]["similarity"]
            assert body["results"][0]["name"] == "Asha Iyer"
            assert body["results"][0]["matched_resume"]["resume_id"] == ml_resume

            # Search with a job's description, and see who already applied to that job.
            acc_job = (
                await client.post("/v1/jobs", json={"title": "Senior accountant", "jd_text": ACCOUNTANT_JD})
            ).json()
            by_job = (await client.post("/v1/talent-pool/search", json={"job_id": acc_job["id"], "limit": 1})).json()
            assert by_job["query_source"] == "job_description"
            assert [x["candidate_id"] for x in by_job["results"]] == [acc] and by_job["results"][0]["in_job"] is None

            # Hiring managers search too, without names.
            manager = await add_user(client, admin, "hiring_manager")
            hm = (await client.post("/v1/talent-pool/search", json={"query": "payroll tax"}, headers=manager)).json()
            assert hm["results"][0]["candidate_id"] == acc and "name" not in hm["results"][0]

            for bad in ({}, {"query": "ab"}, {"query": "python", "limit": 0}, {"query": "python", "limit": 51}):
                assert (await client.post("/v1/talent-pool/search", json=bad)).status_code == 422

    asyncio.run(scenario())


def test_add_found_candidate_to_another_job(settings, embeddings) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            admin = await register(client, companies)
            ml_job = await make_job(client)
            acc_resume = await ingest(svc, client, ml_job, "ravi.txt", ACCOUNTANT_RESUME)
            await drain(svc)
            acc = await candidate_of(client, acc_resume)
            acc_job = (
                await client.post("/v1/jobs", json={"title": "Senior accountant", "jd_text": ACCOUNTANT_JD})
            ).json()

            added = await client.post(f"/v1/jobs/{acc_job['id']}/applications", json={"candidate_id": acc})
            assert added.status_code == 201, added.text
            assert added.json()["stage"] == "applied" and added.json()["created"] is True
            again = await client.post(f"/v1/jobs/{acc_job['id']}/applications", json={"candidate_id": acc})
            assert again.status_code == 200 and again.json()["application_id"] == added.json()["application_id"]
            found = (await client.post("/v1/talent-pool/search", json={"job_id": acc_job["id"]})).json()
            assert found["results"][0]["in_job"]["stage"] == "applied"
            assert found["results"][0]["applications"] == 2
            # A job with confirmed requirements screens the added candidate (consented at upload) with no new upload.
            confirmed_job = await make_job(client)
            assert (
                await client.post(f"/v1/jobs/{confirmed_job}/applications", json={"candidate_id": acc})
            ).status_code == 201
            run = await client.post(f"/v1/jobs/{confirmed_job}/screening-runs")
            assert run.status_code == 202, run.text
            assert run.json()["total"] == 1 and run.json()["blocked"] == []

            manager = await add_user(client, admin, "hiring_manager")
            r = await client.post(f"/v1/jobs/{ml_job}/applications", json={"candidate_id": acc}, headers=manager)
            assert r.status_code == 403
            missing = await client.post(
                f"/v1/jobs/{acc_job['id']}/applications", json={"candidate_id": str(uuid.uuid4())}
            )
            assert missing.status_code == 404

    asyncio.run(scenario())


def test_talent_pool_is_tenant_isolated_and_erasure_removes_vectors(settings, embeddings) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            admin_a = await register(client, companies)
            job_a = await make_job(client)
            ml_resume = await ingest(svc, client, job_a, "asha.txt", ML_RESUME)
            await drain(svc)
            ml = await candidate_of(client, ml_resume)

            export = (await client.get(f"/v1/candidates/{ml}/export")).json()
            assert len(export["search_vectors"]) == 1

            await register(client, companies)  # company B
            job_b = await make_job(client)
            b = (await client.post("/v1/talent-pool/search", json={"query": "python pytorch"})).json()
            assert b["indexed_candidates"] == 0 and b["results"] == []
            cross = await client.post(f"/v1/jobs/{job_b}/applications", json={"candidate_id": ml})
            assert cross.status_code == 404
            cross_job = await client.post("/v1/talent-pool/search", json={"job_id": job_a})
            assert cross_job.status_code == 404

            a = auth(admin_a)
            assert (await client.delete(f"/v1/candidates/{ml}", headers=a)).status_code == 200
            async with svc.db.system_session() as s:
                left = await s.execute(
                    select(CandidateEmbedding).where(CandidateEmbedding.candidate_id == uuid.UUID(ml))
                )
                assert left.first() is None
            after = (await client.post("/v1/talent-pool/search", json={"query": "python"}, headers=a)).json()
            assert ml not in [x["candidate_id"] for x in after["results"]]

    asyncio.run(scenario())


def test_embedding_job_is_retried_and_never_recreates_erased_candidates(settings, embeddings) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            admin = await register(client, companies)
            company = uuid.UUID(admin["company_id"])
            job = await make_job(client)
            embeddings.fail = True  # model server down: ingestion still succeeds, the vector job retries
            resume = await ingest(svc, client, job, "asha.txt", ML_RESUME)
            await drain(svc, limit=3)
            assert (await client.get(f"/v1/resumes/{resume}")).json()["status"] == "ready"
            async with svc.db.system_session() as s:
                pending = (
                    await s.execute(
                        select(QueueJob.status, QueueJob.payload).where(
                            QueueJob.company_id == company, QueueJob.kind == "embed_resume"
                        )
                    )
                ).one()
                assert pending.status == "queued" and pending.payload["resume_id"] == resume
            embeddings.fail = False  # the server is back for the search query; the queued retry has not run yet
            pending_count = (await client.post("/v1/talent-pool/search", json={"query": "python"})).json()
            assert pending_count["resumes_pending_index"] == 1 and pending_count["results"] == []

            # The candidate is erased before the retry: the job is removed and nothing is written.
            candidate = await candidate_of(client, resume)
            assert (await client.delete(f"/v1/candidates/{candidate}")).status_code == 200
            ghost = ClaimedJob(
                id=uuid.uuid4(),
                company_id=company,
                kind="embed_resume",
                payload={"resume_id": resume, "model": talent_pool.embedding_model_id(get_settings())},
                attempts=1,
                max_attempts=5,
            )
            with pytest.raises(NotFound):  # the worker treats NotFound as done
                await talent_pool.process_embed_resume(svc.db, ghost)
            async with svc.db.system_session() as s:
                assert (
                    await s.execute(select(CandidateEmbedding).where(CandidateEmbedding.company_id == company))
                ).first() is None
                kinds = (
                    await s.execute(
                        select(QueueJob.kind).where(QueueJob.company_id == company, QueueJob.status == "queued")
                    )
                ).scalars()
                assert "embed_resume" not in set(kinds)

    asyncio.run(scenario())


def test_reindex_backfills_and_requires_admin(settings, embeddings, monkeypatch) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            admin = await register(client, companies)
            company = uuid.UUID(admin["company_id"])
            job = await make_job(client)
            await ingest(svc, client, job, "asha.txt", ML_RESUME)
            await ingest(svc, client, job, "ravi.txt", ACCOUNTANT_RESUME)
            await drain(svc)
            async with svc.db.system_session() as s:  # e.g. vectors from before an embedding-model upgrade
                await s.execute(delete(CandidateEmbedding).where(CandidateEmbedding.company_id == company))
            recruiter = await add_user(client, admin, "recruiter")
            assert (await client.post("/v1/talent-pool/reindex", headers=recruiter)).status_code == 403
            r = await client.post("/v1/talent-pool/reindex")
            assert r.status_code == 202 and r.json() == {"queued": 2}
            await drain(svc)
            found = (await client.post("/v1/talent-pool/search", json={"query": "python"})).json()
            assert found["indexed_candidates"] == 2

            # Without an embedding model configured, search explains what is needed.
            monkeypatch.setenv("SEMANTIC_BACKEND", "tfidf")
            get_settings.cache_clear()
            off = await client.post("/v1/talent-pool/search", json={"query": "python"})
            assert off.status_code == 409 and "SEMANTIC_BACKEND=embeddings" in off.text

    asyncio.run(scenario())
