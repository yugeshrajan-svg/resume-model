"""Talent-pool matching against PostgreSQL: confirming requirements lists eligible pool candidates for that version.

Only candidates with an active talent-pool consent take part; results are suggestions and never applications.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from backend.models import Application, PoolMatch, QueueJob
from backend.worker import Worker
from tests.test_backend_integration import platform, register
from tests.test_backend_integration import settings as settings
from tests.test_durable_workflow import make_job, upload
from tests.test_hr_management import add_user, auth

pytestmark = pytest.mark.integration
POOL = {"source": "careers form", "purpose": "consideration for current and future roles", "scope": "talent_pool"}
JOB_ONLY = {"source": "careers form", "purpose": "screening for this job"}


async def drain(svc, limit: int = 60) -> None:
    for _ in range(limit):
        if not await Worker(svc).run_once():
            return


async def ingest(svc, client, job_id: str, filename: str, consent: dict) -> str:
    target = await upload(svc, client, filename)
    body = {"blob_name": target["blob_name"], "filename": filename, "job_id": job_id, "consent": consent}
    r = await client.post("/v1/resumes/register", json=body)
    assert r.status_code == 201, r.text
    return r.json()["resume"]["id"]


async def pool_setup(svc, client) -> dict:
    """Three candidates applied to an intake job: two agreed to the talent pool, one only to that job."""
    intake = await make_job(client)
    resumes = {
        "strong": await ingest(svc, client, intake, "01_strong_match.pdf", POOL),
        "intern": await ingest(svc, client, intake, "05_intern.txt", POOL),
        "job_only": await ingest(svc, client, intake, "02_overlapping_jobs.docx", JOB_ONLY),
    }
    await drain(svc)
    ids = {k: (await client.get(f"/v1/resumes/{v}")).json()["candidate_id"] for k, v in resumes.items()}
    return {"intake": intake, **ids}


def test_confirming_requirements_lists_eligible_pool_candidates(settings) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            admin = await register(client, companies)
            c = await pool_setup(svc, client)

            # The intake job's own applicants are not suggested back to it.
            intake = (await client.get(f"/v1/jobs/{c['intake']}/pool-matches")).json()
            assert intake["pool_size"] == 2 and intake["progress"]["total"] == 0

            new_job = await make_job(client)  # confirming v1 queues matching
            await drain(svc)
            r = await client.get(f"/v1/jobs/{new_job}/pool-matches")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["requirements_version"] == 1 and body["confirmed"] is True
            assert body["progress"] == {"total": 2, "checked": 2, "pending": 0, "failed": 0}
            assert [m["candidate_id"] for m in body["eligible"]] == [c["strong"]]
            assert body["eligible"][0]["name"] == "Priya Raman" and body["eligible"][0]["in_job"] is None
            assert [m["candidate_id"] for m in body["ineligible"]] == [c["intern"]]
            assert any("required skills" in reason for reason in body["ineligible"][0]["reasons"])
            listed = {m["candidate_id"] for m in body["eligible"] + body["ineligible"]}
            assert c["job_only"] not in listed  # consent only for the intake job

            # Suggestions never create applications.
            async with svc.db.system_session() as s:
                apps = await s.execute(select(Application).where(Application.job_id == uuid.UUID(new_job)))
                assert apps.first() is None

            # Hiring managers see the list without names and cannot refresh it.
            manager = await add_user(client, admin, "hiring_manager")
            hm = (await client.get(f"/v1/jobs/{new_job}/pool-matches", headers=manager)).json()
            assert hm["eligible"][0]["candidate_id"] == c["strong"] and "name" not in hm["eligible"][0]
            assert (await client.post(f"/v1/jobs/{new_job}/pool-matches/refresh", headers=manager)).status_code == 403

            # Add every eligible candidate in one step; the list then shows them as applied.
            added = await client.post(
                f"/v1/jobs/{new_job}/applications/bulk",
                json={"candidate_ids": [m["candidate_id"] for m in body["eligible"]] + [str(uuid.uuid4())]},
            )
            assert added.status_code == 200, added.text
            assert added.json()["added"] == [c["strong"]] and len(added.json()["failed"]) == 1
            after = (await client.get(f"/v1/jobs/{new_job}/pool-matches")).json()
            assert after["eligible"][0]["in_job"] == "applied"
            run = await client.post(f"/v1/jobs/{new_job}/screening-runs")
            assert run.status_code == 202 and run.json()["total"] == 1

    asyncio.run(scenario())


def test_each_requirements_version_has_its_own_eligibility(settings) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            await register(client, companies)
            c = await pool_setup(svc, client)
            job = await make_job(client)
            await drain(svc)
            v1 = (await client.get(f"/v1/jobs/{job}/requirements")).json()[0]["requirements"]
            stricter = {**v1, "experience_min_years": 40}
            assert (await client.put(f"/v1/jobs/{job}/requirements", json=stricter)).status_code == 201
            confirm = await client.post(f"/v1/jobs/{job}/requirements/confirm", json={"version": 2})
            assert confirm.status_code == 200
            await drain(svc)

            latest = (await client.get(f"/v1/jobs/{job}/pool-matches")).json()
            assert latest["requirements_version"] == 2 and latest["eligible"] == []
            strong = next(m for m in latest["ineligible"] if m["candidate_id"] == c["strong"])
            assert any("minimum is 40" in reason for reason in strong["reasons"])
            first = (await client.get(f"/v1/jobs/{job}/pool-matches", params={"version": 1})).json()
            assert [m["candidate_id"] for m in first["eligible"]] == [c["strong"]]
            assert (await client.get(f"/v1/jobs/{job}/pool-matches", params={"version": 9})).status_code == 404

            # A version that is not confirmed yet has no matches.
            assert (await client.put(f"/v1/jobs/{job}/requirements", json=v1)).status_code == 201
            draft = (await client.get(f"/v1/jobs/{job}/pool-matches", params={"version": 3})).json()
            assert draft["confirmed"] is False and draft["eligible"] == []

    asyncio.run(scenario())


def test_consent_controls_the_pool_and_erasure_removes_matches(settings) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            admin = await register(client, companies)
            c = await pool_setup(svc, client)
            job = await make_job(client)
            await drain(svc)

            # Granting talent-pool consent later brings a candidate in on the next refresh.
            grant = await client.post(f"/v1/candidates/{c['job_only']}/consents", json=POOL)
            assert grant.status_code == 201
            consents = (await client.get(f"/v1/candidates/{c['job_only']}/consents")).json()["consents"]
            assert {item["scope"] for item in consents} == {"job", "talent_pool"}
            assert (await client.post(f"/v1/jobs/{job}/pool-matches/refresh")).json() == {"requirements_version": 1}
            await drain(svc)
            body = (await client.get(f"/v1/jobs/{job}/pool-matches")).json()
            assert body["pool_size"] == 3 and body["progress"]["checked"] == 3

            # Withdrawing consent removes the candidate from every list immediately.
            assert (await client.post(f"/v1/candidates/{c['strong']}/consents/revoke")).status_code == 200
            body = (await client.get(f"/v1/jobs/{job}/pool-matches")).json()
            listed = {m["candidate_id"] for m in body["eligible"] + body["ineligible"] + body["failed"]}
            assert c["strong"] not in listed and body["pool_size"] == 2

            export = (await client.get(f"/v1/candidates/{c['intern']}/export")).json()
            assert [m["candidate_id"] for m in export["pool_matches"]] == [c["intern"]]
            assert (await client.delete(f"/v1/candidates/{c['intern']}")).status_code == 200
            async with svc.db.system_session() as s:
                left = await s.execute(select(PoolMatch).where(PoolMatch.candidate_id == uuid.UUID(c["intern"])))
                assert left.first() is None

            # Another company sees nothing of this pool.
            await register(client, companies)
            assert (await client.get(f"/v1/jobs/{job}/pool-matches")).status_code == 404
            assert (await client.post(f"/v1/jobs/{job}/pool-matches/refresh")).status_code == 404
            other = await make_job(client)
            await drain(svc)
            assert (await client.get(f"/v1/jobs/{other}/pool-matches")).json()["pool_size"] == 0
            cross = await client.post(f"/v1/jobs/{other}/applications/bulk", json={"candidate_ids": [c["job_only"]]})
            assert cross.json()["added"] == [] and len(cross.json()["failed"]) == 1
            a = auth(admin)
            assert (await client.get(f"/v1/jobs/{job}/pool-matches", headers=a)).status_code == 200

    asyncio.run(scenario())


def test_uploads_are_processed_before_queued_pool_work(settings) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            admin = await register(client, companies)
            company = uuid.UUID(admin["company_id"])
            await pool_setup(svc, client)
            job = await make_job(client)  # queues talent-pool matching (background priority)
            resume = await ingest(svc, client, job, "07_location_notice.txt", POOL)  # queued later, interactive
            assert await Worker(svc).run_once()
            assert (await client.get(f"/v1/resumes/{resume}")).json()["status"] == "ready"
            async with svc.db.system_session() as s:
                pending = await s.execute(
                    select(QueueJob.kind).where(QueueJob.company_id == company, QueueJob.status == "queued")
                )
                assert "match_pool" in set(pending.scalars())

    asyncio.run(scenario())
