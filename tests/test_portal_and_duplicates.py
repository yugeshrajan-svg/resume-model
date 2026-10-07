import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from backend.models import (
    Application,
    Candidate,
    CandidateContact,
    Consent,
    ErasureTombstone,
    JobRequirementsVersion,
    QueueJob,
    Resume,
    ResumeRegistration,
    ScreeningInput,
    ScreeningRun,
)
from backend.security import decode_access_token
from backend.services.resumes import _find_duplicate
from tests.test_backend_integration import platform, prepare_run, register
from tests.test_backend_integration import settings as settings

pytestmark = pytest.mark.integration


def test_public_applications_and_single_use_booking_are_scoped_and_consentful(settings):
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            token = await register(client, companies)
            headers = {"Authorization": "Bearer " + token["access_token"]}
            job, _, _, candidate = await prepare_run(svc, client)
            published = await client.post("/v1/portal/jobs", json={"job_id": job, "confirmed": True})
            assert published.status_code == 201, published.text
            page = published.json()["id"]
            start = datetime.now(UTC) + timedelta(days=1)
            booking = await client.post(
                "/v1/portal/booking-links",
                json={
                    "candidate_id": candidate,
                    "job_id": job,
                    "assigned_to": token["user_id"],
                    "slots": [{"start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat()}],
                    "location": "Office",
                },
            )
            assert booking.status_code == 201, booking.text
            link = booking.json()["id"]
            client.headers.pop("Authorization")
            assert (await client.get(f"/v1/public/jobs/{page}")).json()["title"] == "ML Engineer"
            assert (
                await client.post(f"/v1/public/jobs/{page}/apply", content=b"CV", headers={"x-filename": "cv.txt"})
            ).status_code == 422
            submitted = await client.post(
                f"/v1/public/jobs/{page}/apply",
                content=b"Resume of Applicant. Python engineering experience.",
                headers={"x-filename": "applicant.txt", "x-candidate-consent": "yes"},
            )
            assert submitted.status_code == 202, submitted.text
            assert submitted.json() == {"received": True}
            available = await client.get(f"/v1/public/book/{link}")
            assert available.status_code == 200 and len(available.json()["slots"]) == 1
            assert "candidate_id" not in available.json()
            booked = await client.post(f"/v1/public/book/{link}", json={"index": 0})
            assert booked.status_code == 200, booked.text
            assert (await client.post(f"/v1/public/book/{link}", json={"index": 0})).status_code == 410
            assert (await client.get(f"/v1/public/book/{link}")).status_code == 410
            client.headers.update(headers)
            principal = decode_access_token(headers["Authorization"].split()[1], svc.settings)
            async with svc.tenant(principal) as repo:
                registrations = await repo.list(ResumeRegistration, ResumeRegistration.job_id == uuid.UUID(job))
                public = next(r for r in registrations if r.source_type == "public_application")
                assert public.recorded_by is None and public.consent["scope"] == "job"
                assert public.consent["source"] == "Candidate submitted public application"
            recorded = (await client.get("/v1/recruiting/activities", params={"kind": "interview"})).json()["items"][0]
            assert recorded["created_by"] is None
            assert (await client.post(f"/v1/portal/links/{page}/close")).status_code == 200
            assert (await client.get(f"/v1/public/jobs/{page}")).status_code == 410
            await register(client, companies)
            assert (await client.get("/v1/portal/links")).json() == []
            assert (await client.post(f"/v1/portal/links/{page}/close")).status_code == 404

    asyncio.run(scenario())


def test_consolidation_preserves_history_aliases_and_blocks_overlapping_applications(settings):
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            await register(client, companies)
            principal = decode_access_token(client.headers["Authorization"].split()[1], svc.settings)
            job, run_id, _, target = await prepare_run(svc, client)
            source = uuid.uuid4()
            source_resume = uuid.uuid4()
            source_job = (
                await client.post(
                    "/v1/jobs",
                    json={"title": "Second role", "jd_text": "Python engineer building reliable backend services"},
                )
            ).json()["id"]
            blob = str(principal.company_id) + "/manual-source.txt"
            data = b"Second resume for explicitly verified person"
            await svc.storage.put(blob, data)
            async with svc.tenant(principal) as repo:
                existing = await repo.get(
                    CandidateContact,
                    (await repo.find(CandidateContact, CandidateContact.candidate_id == uuid.UUID(target))).id,
                )
                repo.add(Candidate(id=source))
                await repo.flush()
                repo.add(
                    CandidateContact(
                        candidate_id=source, name=existing.name, email="alternate@example.com", email_hash="f" * 64
                    )
                )
                repo.add(
                    Resume(
                        id=source_resume,
                        candidate_id=source,
                        version=1,
                        filename="manual-source.txt",
                        blob_name=blob,
                        blob_uri=svc.storage.uri(blob),
                        content_hash=hashlib.sha256(data).hexdigest(),
                        mime="text/plain",
                        size=len(data),
                        status="ready",
                        redacted_text="Verified person",
                        processed_generation=1,
                    )
                )
                await repo.flush()
                source_app = repo.add(
                    Application(job_id=uuid.UUID(source_job), candidate_id=source, resume_id=source_resume)
                )
                repo.add(
                    Consent(
                        candidate_id=source,
                        source="Recorded application",
                        purpose="Recruitment",
                        scope="job",
                        granted_at=datetime.now(UTC),
                    )
                )
                run = await repo.get(ScreeningRun, uuid.UUID(run_id), for_update=True)
                run.status = "completed"
                run.done = run.total
                original_requirements = await repo.get(JobRequirementsVersion, run.job_requirements_id)
                requirements = repo.add(
                    JobRequirementsVersion(
                        job_id=uuid.UUID(source_job),
                        version=1,
                        requirements=original_requirements.requirements,
                        source="manual",
                    )
                )
                await repo.flush()
                source_run = repo.add(
                    ScreeningRun(
                        job_id=uuid.UUID(source_job),
                        job_requirements_id=requirements.id,
                        requirements_version=1,
                        extraction_mode=run.extraction_mode,
                        semantic_backend=run.semantic_backend,
                        versions=run.versions,
                        weights=run.weights,
                        snapshot=run.snapshot,
                        reference_date=run.reference_date,
                        status="completed",
                        total=1,
                        done=1,
                        blocked=[],
                    )
                )
                await repo.flush()
                frozen = repo.add(
                    ScreeningInput(
                        run_id=source_run.id,
                        application_id=source_app.id,
                        candidate_id=source,
                        resume_id=source_resume,
                        resume_version=1,
                        processing_generation=1,
                        content_hash=hashlib.sha256(data).hexdigest(),
                        redacted_text="Verified person",
                    )
                )
                await repo.session.execute(
                    update(QueueJob)
                    .where(QueueJob.company_id == repo.company_id, QueueJob.payload["run_id"].astext == run_id)
                    .values(status="done")
                )
                await repo.flush()
                app_id = source_app.id
                frozen_id = frozen.id
            groups = (await client.get("/v1/talent-pool/duplicates")).json()
            assert groups["total"] == 1
            body = {
                "source_id": str(source),
                "target_id": target,
                "reason": "Verified original files and alternate identity",
                "confirmed": True,
            }
            assert (await client.post("/v1/candidates/merge/preview", json=body)).json()["can_merge"]
            async with svc.tenant(principal) as repo:
                app = await repo.get(Application, app_id)
                app.job_id = uuid.UUID(job)
            assert (await client.post("/v1/candidates/merge", json=body)).status_code == 409
            async with svc.tenant(principal) as repo:
                app = await repo.get(Application, app_id)
                app.job_id = uuid.UUID(source_job)
            moved = await client.post("/v1/candidates/merge", json=body)
            assert moved.status_code == 200, moved.text
            assert (await client.get(f"/v1/candidates/{source}")).status_code == 404
            export = (await client.get(f"/v1/candidates/{target}/export")).json()
            assert len(export["resumes"]) == 2 and len(export["applications"]) == 2 and len(export["consents"]) == 2
            assert any(r["resume_id"] == str(source_resume) for r in export["resume_files"])
            assert export["identity_aliases"]
            preserved = next(row for row in export["screening_inputs"] if row["id"] == str(frozen_id))
            assert preserved["candidate_id"] == target
            assert preserved["application_id"] == str(app_id) and preserved["resume_version"] == 1
            assert preserved["redacted_text"] == "Verified person"
            async with svc.tenant(principal) as repo:
                assert (await _find_duplicate(repo, "f" * 64, None)).id == uuid.UUID(target)
            assert (await client.delete(f"/v1/candidates/{target}")).status_code == 200
            assert not await svc.storage.exists(blob)
            async with svc.tenant(principal) as repo:
                assert await repo.find(ErasureTombstone, ErasureTombstone.contact_hash == "f" * 64)

    asyncio.run(scenario())
