"""Source selection, frozen versions, consent withdrawal and company isolation on real PostgreSQL."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from backend.models import Application, Resume
from engine.parsing import parse_bytes
from tests.conftest import SAMPLE_DIR
from tests.test_backend_integration import platform, register
from tests.test_backend_integration import settings as settings
from tests.test_durable_workflow import make_job, upload
from tests.test_hr_management import add_user, auth
from tests.test_pool_matching import JOB_ONLY, POOL, drain, ingest, pool_setup

pytestmark = pytest.mark.integration


async def duplicate_upload(svc, client, job: str, consent: dict) -> str:
    target = await upload(svc, client, "01_strong_match.pdf")
    response = await client.post(
        "/v1/resumes/register",
        json={"blob_name": target["blob_name"], "filename": "01_strong_match.pdf", "job_id": job, "consent": consent},
    )
    assert response.status_code == 200 and response.json()["created"] is False, response.text
    return response.json()["resume"]["id"]


@pytest.mark.parametrize("source", ["database", "uploads", "both"])
def test_sources_select_candidates_and_deduplicate_without_reclassifying_saved_applicants(settings, source):
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            await register(client, companies)
            pool = await pool_setup(svc, client)
            job = await make_job(client)
            # One existing pool candidate is also uploaded to this job; one upload has job consent only.
            await duplicate_upload(svc, client, job, POOL)
            uploaded_id = await ingest(svc, client, job, "07_location_notice.txt", JOB_ONLY)
            await drain(svc)
            uploaded = (await client.get(f"/v1/resumes/{uploaded_id}")).json()["candidate_id"]
            preview = await client.get(f"/v1/jobs/{job}/screening-sources")
            assert preview.status_code == 200, preview.text
            counts = preview.json()
            assert counts["database"] == {"total": 2, "screenable": 2, "blocked": 0}
            assert counts["uploads"] == {"total": 2, "screenable": 2, "blocked": 0}
            assert counts["both"] == {"total": 3, "screenable": 3, "blocked": 0}
            assert counts["database_excluded"] == 2 and counts["pending_uploads"] == 0
            async with svc.db.system_session() as session:
                before = list(
                    (await session.execute(select(Application).where(Application.job_id == uuid.UUID(job)))).scalars()
                )
                assert len(before) == 2  # The preview never attaches saved resumes.
            response = await client.post(f"/v1/jobs/{job}/screening-runs", json={"source": source})
            assert response.status_code == 202, response.text
            run = response.json()
            expected = {
                "database": {pool["strong"], pool["intern"]},
                "uploads": {pool["strong"], uploaded},
                "both": {pool["strong"], pool["intern"], uploaded},
            }[source]
            assert run["total"] == len(expected)
            assert run["snapshot"]["resume_source"] == source
            inputs = (await client.get(f"/v1/screening-runs/{run['id']}/inputs")).json()
            assert len(inputs) == len(expected)
            assert {i["candidate_id"] for i in inputs} == expected
            await drain(svc)
            completed = (await client.get(f"/v1/screening-runs/{run['id']}")).json()
            assert completed["status"] == "completed" and completed["done"] == len(expected)
            results = (await client.get(f"/v1/jobs/{job}/candidates", params={"run_id": run["id"]})).json()
            assert {i["candidate_id"] for i in results["items"]} == expected
            if source != "uploads":
                assert next(i for i in results["items"] if i["candidate_id"] == pool["intern"])["eligible"] is False
            after = (await client.get(f"/v1/jobs/{job}/screening-sources")).json()
            assert after == counts  # Database-created applications do not become uploads next time.

    asyncio.run(scenario())


@pytest.mark.parametrize("source", ["database", "both"])
def test_saved_consent_scope_survives_withdrawal_later_uploads_and_retries(settings, source):
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            await register(client, companies)
            pool = await pool_setup(svc, client)
            job = await make_job(client)
            response = await client.post(f"/v1/jobs/{job}/screening-runs", json={"source": source})
            assert response.status_code == 202, response.text
            run = response.json()
            # Revoke pool consent, then upload the same resume with job consent. The already frozen saved input
            # must still require pool consent, including in a retry.
            assert (await client.post(f"/v1/candidates/{pool['strong']}/consents/revoke")).status_code == 200
            await duplicate_upload(svc, client, job, JOB_ONLY)
            await drain(svc)
            results = (await client.get(f"/v1/jobs/{job}/candidates", params={"run_id": run["id"]})).json()
            strong = next(i for i in results["items"] if i["candidate_id"] == pool["strong"])
            assert strong["status"] == "needs_reprocess" and "consent" in strong["reprocess_reason"]
            retry = await client.post(f"/v1/screening-runs/{run['id']}/retry")
            assert retry.status_code == 409
            assert (await client.post(f"/v1/candidates/{pool['strong']}/consents", json=POOL)).status_code == 201
            retry = await client.post(f"/v1/screening-runs/{run['id']}/retry")
            assert retry.status_code == 202, retry.text
            assert retry.json()["total"] == 1
            assert retry.json()["snapshot"]["resume_source"] == source
            assert retry.json()["snapshot"]["source_selected_at"] == run["snapshot"]["source_selected_at"]
            await drain(svc)
            done = (await client.get(f"/v1/screening-runs/{retry.json()['id']}")).json()
            assert done["status"] == "completed" and done["done"] == 1

    asyncio.run(scenario())


def test_saved_source_uses_latest_ready_version_and_preserves_manual_decisions(settings):
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            await register(client, companies)
            pool = await pool_setup(svc, client)
            job = await make_job(client)
            original_upload = await duplicate_upload(svc, client, job, POOL)
            applied = await client.post(f"/v1/jobs/{job}/applications", json={"candidate_id": pool["strong"]})
            app_id = applied.json()["application_id"]
            assert (
                await client.post(
                    f"/v1/applications/{app_id}/decision", json={"decision": "review", "reason": "Interview booked"}
                )
            ).status_code == 201
            text = parse_bytes((SAMPLE_DIR / "resumes/01_strong_match.pdf").read_bytes(), "sample.pdf").text
            target = await upload(svc, client, "updated.txt", (text + "\nSkills: Rust\n").encode())
            new = await client.post(
                "/v1/resumes/register",
                json={"blob_name": target["blob_name"], "filename": "updated.txt", "consent": POOL},
            )
            assert new.status_code == 201, new.text
            await drain(svc)
            resume = (await client.get(f"/v1/resumes/{new.json()['resume']['id']}")).json()
            assert resume["candidate_id"] == pool["strong"] and resume["version"] == 2
            response = await client.post(f"/v1/jobs/{job}/screening-runs", json={"source": "database"})
            assert response.status_code == 202, response.text
            inputs = (await client.get(f"/v1/screening-runs/{response.json()['id']}/inputs")).json()
            strong = next(i for i in inputs if i["candidate_id"] == pool["strong"])
            assert strong["resume_id"] == resume["id"] and strong["resume_version"] == 2
            await drain(svc)
            async with svc.db.system_session() as session:
                app = await session.get(Application, uuid.UUID(app_id))
                assert app.stage == "review" and app.resume_id == uuid.UUID(resume["id"])
                assert (await session.get(Resume, uuid.UUID(resume["id"]))).status == "ready"
            preview = (await client.get(f"/v1/jobs/{job}/screening-sources")).json()
            assert preview["uploads"] == {"total": 1, "screenable": 1, "blocked": 0}
            uploaded_run = await client.post(f"/v1/jobs/{job}/screening-runs", json={"source": "uploads"})
            assert uploaded_run.status_code == 202 and uploaded_run.json()["total"] == 1, uploaded_run.text
            uploaded_inputs = (await client.get(f"/v1/screening-runs/{uploaded_run.json()['id']}/inputs")).json()
            assert uploaded_inputs[0]["resume_id"] == original_upload and uploaded_inputs[0]["resume_version"] == 1
            await drain(svc)
            async with svc.db.system_session() as session:
                assert (await session.get(Application, uuid.UUID(app_id))).stage == "review"

    asyncio.run(scenario())


def test_pending_uploads_must_finish_before_upload_or_combined_screening(settings):
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            await register(client, companies)
            job = await make_job(client)
            await ingest(svc, client, job, "07_location_notice.txt", JOB_ONLY)
            preview = (await client.get(f"/v1/jobs/{job}/screening-sources")).json()
            assert preview["pending_uploads"] == 1
            for source in ["uploads", "both"]:
                blocked = await client.post(f"/v1/jobs/{job}/screening-runs", json={"source": source})
                assert blocked.status_code == 409 and "finish processing" in blocked.json()["detail"]
            await drain(svc)
            started = await client.post(f"/v1/jobs/{job}/screening-runs", json={"source": "uploads"})
            assert started.status_code == 202 and started.json()["total"] == 1

    asyncio.run(scenario())


def test_sources_are_tenant_scoped_permission_checked_and_validated(settings):
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            first = await register(client, companies)
            await pool_setup(svc, client)
            first_job = await make_job(client)
            manager = await add_user(client, first, "hiring_manager")
            view = await client.get(f"/v1/jobs/{first_job}/screening-sources", headers=manager)
            assert view.status_code == 200 and view.json()["database"]["total"] == 2
            assert (
                await client.post(f"/v1/jobs/{first_job}/screening-runs", json={"source": "database"}, headers=manager)
            ).status_code == 403
            await register(client, companies)
            second_job = await make_job(client)
            view = await client.get(f"/v1/jobs/{second_job}/screening-sources")
            assert all(view.json()[source]["total"] == 0 for source in ["database", "uploads", "both"])
            assert (
                await client.post(f"/v1/jobs/{second_job}/screening-runs", json={"source": "database"})
            ).status_code == 409
            for path, method, body in [
                ("screening-sources", "GET", None),
                ("screening-runs", "POST", {"source": "both"}),
            ]:
                r = await client.request(method, f"/v1/jobs/{second_job}/{path}", headers=auth(first), json=body)
                assert r.status_code == 404
            for body in [{}, {"source": "all_companies"}, {"source": "database", "company_id": first["company_id"]}]:
                assert (await client.post(f"/v1/jobs/{second_job}/screening-runs", json=body)).status_code == 422

    asyncio.run(scenario())
