"""HR management against PostgreSQL: roles, tenant isolation, decisions, comparison, consent, export, erasure,
retention races and dashboard settings/usage. Opt-in like the other integration tests (see README)."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import column, select, table, text, update
from sqlalchemy.exc import DBAPIError

from backend.models import (
    AuditLog,
    Base,
    Candidate,
    Decision,
    ErasureTombstone,
    ExtractionCacheEntry,
    QueueJob,
    Resume,
    ScreeningInput,
    ScreeningResult,
    UsageEvent,
)
from backend.services import screening
from backend.worker import Worker, schedule_retention
from tests.conftest import SAMPLE_DIR
from tests.test_backend_integration import platform, prepare_run, register
from tests.test_backend_integration import settings as settings
from tests.test_durable_workflow import item, make_job, upload

pytestmark = pytest.mark.integration
PASSWORD = "Integration-password-123"
EMAIL = "priya.raman@example.com"  # contact in data/sample/resumes/01_strong_match.pdf
SAME_PERSON_V2 = (
    b"Priya Raman\npriya.raman@example.com\nLocation: Bengaluru, India\nNotice period: 30 days\n\n"
    b"Experience\nSenior Machine Learning Engineer | Nimbus Analytics | Jan 2021 - Present\n"
    b"- Built RAG pipelines with LangChain and PyTorch\n\nSkills\nPython, PyTorch, SQL\n"
)


def auth(token: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {token['access_token']}"}


async def add_user(client, admin: dict, role: str) -> dict[str, str]:
    email = f"{uuid.uuid4().hex}@example.com"
    r = await client.post("/v1/users", json={"email": email, "password": PASSWORD, "role": role}, headers=auth(admin))
    assert r.status_code == 201, r.text
    login = await client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return {**auth(login.json()), "X-Email": email}


async def drain(svc, limit: int = 50) -> int:
    processed = 0
    while processed < limit and await Worker(svc).run_once():
        processed += 1
    return processed


async def screened_application(svc, client, headers=None) -> dict:
    """A completed one-candidate run: ids of job, run, resume, candidate and application."""
    job_id, run_id, resume_id, candidate_id = await prepare_run(svc, client)
    await drain(svc)
    page = (await client.get(f"/v1/jobs/{job_id}/candidates", headers=headers)).json()
    return {
        "job_id": job_id,
        "run_id": run_id,
        "resume_id": resume_id,
        "candidate_id": candidate_id,
        "application_id": page["items"][0]["application_id"],
    }


# ---------------------------------------------------------------------------------------------- roles and isolation


def test_role_permissions_and_restricted_contact_visibility(settings) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            admin = await register(client, companies)
            ids = await screened_application(svc, client)
            recruiter = await add_user(client, admin, "recruiter")
            manager = await add_user(client, admin, "hiring_manager")
            cid, app_id, job_id = ids["candidate_id"], ids["application_id"], ids["job_id"]
            other = (await client.get(f"/v1/jobs/{job_id}/candidates")).json()["items"][0]["candidate_id"]

            forbidden = {
                "hiring_manager": [
                    ("GET", "/v1/candidates?email=x@example.com", None),
                    ("POST", f"/v1/candidates/{cid}/consents", {"source": "form", "purpose": "screening"}),
                    ("POST", f"/v1/candidates/{cid}/consents/revoke", None),
                    ("GET", f"/v1/candidates/{cid}/export", None),
                    ("DELETE", f"/v1/candidates/{cid}", None),
                    ("GET", "/v1/company", None),
                    ("PATCH", "/v1/company", {"retention_days": 30}),
                    ("GET", "/v1/usage", None),
                    ("GET", "/v1/users", None),
                    ("POST", f"/v1/jobs/{job_id}/screening-runs", None),
                ],
                "recruiter": [
                    ("GET", f"/v1/candidates/{cid}/export", None),
                    ("DELETE", f"/v1/candidates/{cid}", None),
                    ("GET", "/v1/company", None),
                    ("PATCH", "/v1/company", {"retention_days": 30}),
                    ("GET", "/v1/usage", None),
                    ("GET", "/v1/users", None),
                ],
            }
            for role, headers in (("hiring_manager", manager), ("recruiter", recruiter)):
                for method, path, body in forbidden[role]:
                    r = await client.request(method, path, json=body, headers=headers)
                    assert r.status_code == 403, (role, method, path, r.status_code)

            # Every role may read candidates, compare, view decision history and record decisions.
            for headers in (manager, recruiter, auth(admin)):
                for path in (f"/v1/candidates/{cid}", f"/v1/candidates/{cid}/consents",
                             f"/v1/applications/{app_id}/decisions", f"/v1/jobs/{job_id}/candidates"):  # fmt: skip
                    assert (await client.get(path, headers=headers)).status_code == 200, path
            decision = await client.post(
                f"/v1/applications/{app_id}/decision", json={"decision": "review"}, headers=manager
            )
            assert decision.status_code == 201, decision.text
            assert other == cid

            # Contact details (and resume filenames, which often contain names) are hidden from hiring managers.
            profile = (await client.get(f"/v1/candidates/{cid}", headers=manager)).json()
            assert "contact" not in profile
            assert all(r["filename"] is None for r in profile["resumes"])
            resume = (await client.get(f"/v1/resumes/{ids['resume_id']}", headers=manager)).json()
            assert resume["filename"] is None
            for path in (f"/v1/candidates/{cid}", f"/v1/jobs/{job_id}/candidates", f"/v1/candidates/{cid}/consents"):
                body = (await client.get(path, headers=manager)).text.lower()
                assert EMAIL not in body and "priya" not in body and "90000" not in body, path
            recruiter_view = (await client.get(f"/v1/candidates/{cid}", headers=recruiter)).json()
            assert recruiter_view["contact"]["email"] == EMAIL
            assert recruiter_view["resumes"][0]["filename"] == "01_strong_match.pdf"
            assert (await client.get(f"/v1/resumes/{ids['resume_id']}", headers=recruiter)).json()["filename"]

    asyncio.run(scenario())


def test_cross_company_reads_writes_and_searches_are_denied(settings) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            admin_a = await register(client, companies)
            a = await screened_application(svc, client)
            r = await client.post(f"/v1/applications/{a['application_id']}/decision", json={"decision": "shortlist"})
            assert r.status_code == 201
            async with svc.db.system_session() as s:
                s.add(UsageEvent(company_id=uuid.UUID(admin_a["company_id"]), kind="llm", prompt_tokens=500,
                                 cost_usd=0.5))  # fmt: skip

            admin_b = await register(client, companies)  # client now authenticates as company B's admin
            b_job = await make_job(client)
            cid, app_id = a["candidate_id"], a["application_id"]
            denied = [
                ("GET", f"/v1/candidates/{cid}", None),
                ("GET", f"/v1/candidates/{cid}/consents", None),
                ("POST", f"/v1/candidates/{cid}/consents", {"source": "form", "purpose": "screening"}),
                ("POST", f"/v1/candidates/{cid}/consents/revoke", None),
                ("GET", f"/v1/candidates/{cid}/export", None),
                ("DELETE", f"/v1/candidates/{cid}", None),
                ("GET", f"/v1/applications/{app_id}/decisions", None),
                ("POST", f"/v1/applications/{app_id}/decision", {"decision": "reject", "reason": "x"}),
                ("POST", "/v1/candidates/compare", {"job_id": a["job_id"], "candidate_ids": [cid, str(uuid.uuid4())]}),
                ("POST", "/v1/candidates/compare", {"job_id": b_job, "candidate_ids": [cid, str(uuid.uuid4())]}),
                ("GET", f"/v1/resumes/{a['resume_id']}", None),
                ("GET", f"/v1/screening-runs/{a['run_id']}", None),
                ("GET", f"/v1/jobs/{a['job_id']}/candidates", None),
            ]
            for method, path, body in denied:
                r = await client.request(method, path, json=body)
                assert r.status_code == 404, (method, path, r.status_code, r.text)
            assert (await client.get("/v1/candidates", params={"email": EMAIL})).json() == []
            assert (await client.get("/v1/company")).json()["id"] == admin_b["company_id"]
            assert (await client.get("/v1/usage")).json()["totals"]["prompt_tokens"] == 0

            # Company A's data is untouched by B's attempts.
            history = (await client.get(f"/v1/applications/{app_id}/decisions", headers=auth(admin_a))).json()
            assert [d["decision"] for d in history["decisions"]] == ["shortlist"]
            consents = (await client.get(f"/v1/candidates/{cid}/consents", headers=auth(admin_a))).json()
            assert consents["has_active_consent"] is True
            assert (await client.get("/v1/usage", headers=auth(admin_a))).json()["totals"]["prompt_tokens"] == 500

    asyncio.run(scenario())


# ---------------------------------------------------------------------------------------------- decisions


def test_decision_history_and_manual_decisions_survive_screening(settings) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            admin = await register(client, companies)
            recruiter = await add_user(client, admin, "recruiter")
            manager = await add_user(client, admin, "hiring_manager")
            job_id, run_id, _, candidate_id = await prepare_run(svc, client)
            profile = (await client.get(f"/v1/candidates/{candidate_id}")).json()
            app_id = profile["applications"][0]["application_id"]

            # A decision made before the run is processed survives the run's completion.
            r = await client.post(f"/v1/applications/{app_id}/decision",
                                  json={"decision": "shortlist", "reason": "Strong ML platform work"},
                                  headers=recruiter)  # fmt: skip
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["stage"] == "shortlisted" and body["previous_stage"] == "applied"
            assert body["reviewer"]["role"] == "recruiter" and body["decided_at"]
            await drain(svc)
            run = (await client.get(f"/v1/screening-runs/{run_id}")).json()
            assert run["status"] == "completed"
            assert (await client.get(f"/v1/applications/{app_id}/decisions")).json()["stage"] == "shortlisted"

            await client.post(f"/v1/applications/{app_id}/decision", json={"decision": "review", "reason": "Panel"},
                              headers=manager)  # fmt: skip
            await client.post(f"/v1/applications/{app_id}/decision", json={"decision": "reject", "reason": "Filled"})
            history = (await client.get(f"/v1/applications/{app_id}/decisions")).json()
            assert history["stage"] == "rejected"
            decisions = history["decisions"]
            assert [d["decision"] for d in decisions] == ["reject", "review", "shortlist"]
            assert [(d["previous_stage"], d["new_stage"]) for d in decisions] == [
                ("review", "rejected"), ("shortlisted", "review"), ("applied", "shortlisted"),
            ]  # fmt: skip
            assert [d["reviewer"]["role"] for d in decisions] == ["admin", "hiring_manager", "recruiter"]
            assert decisions[1]["reviewer"]["email"] == manager["X-Email"]
            assert [d["reason"] for d in decisions] == ["Filled", "Panel", "Strong ML platform work"]
            times = [datetime.fromisoformat(d["decided_at"]) for d in decisions]
            assert times == sorted(times, reverse=True)
            assert len((await client.get(f"/v1/candidates/{candidate_id}")).json()["applications"][0]["decisions"]) == 3

            # A new run of the same job (new requirements version) keeps the manual decision.
            new_run = await client.post(f"/v1/jobs/{job_id}/screening-runs")
            assert new_run.status_code == 202, new_run.text
            await drain(svc)
            assert (await client.get(f"/v1/screening-runs/{new_run.json()['id']}")).json()["status"] == "completed"
            assert (await client.get(f"/v1/applications/{app_id}/decisions")).json()["stage"] == "rejected"
            page = (await client.get(f"/v1/jobs/{job_id}/candidates", params={"stage": "rejected"})).json()
            assert page["total"] == 1

            assert (
                await client.post(f"/v1/applications/{app_id}/decision", json={"decision": "hire"})
            ).status_code == 422
            # History is append-only for the application database role.
            company_id = uuid.UUID(admin["company_id"])
            with pytest.raises(DBAPIError, match="permission denied"):
                async with svc.db.tenant_session(company_id) as s:
                    await s.execute(update(Decision).where(Decision.company_id == company_id).values(reason="edited"))

    asyncio.run(scenario())


def test_decision_made_while_screening_is_in_flight_is_not_overwritten(settings, monkeypatch) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            await register(client, companies)
            _, run_id, _, candidate_id = await prepare_run(svc, client)
            app_id = (await client.get(f"/v1/candidates/{candidate_id}")).json()["applications"][0]["application_id"]
            started, release = threading.Event(), threading.Event()
            original = screening.assess

            def paused(*args):
                started.set()
                assert release.wait(timeout=10)
                return original(*args)

            monkeypatch.setattr(screening, "assess", paused)
            work = asyncio.create_task(Worker(svc).run_once())
            try:
                assert await asyncio.to_thread(started.wait, 10)
                r = await client.post(f"/v1/applications/{app_id}/decision", json={"decision": "shortlist"})
                assert r.status_code == 201
            finally:
                release.set()
                assert await work
            assert (await client.get(f"/v1/screening-runs/{run_id}")).json()["status"] == "completed"
            history = (await client.get(f"/v1/applications/{app_id}/decisions")).json()
            assert history["stage"] == "shortlisted"  # the run's automatic "screened" stage did not overwrite it

    asyncio.run(scenario())


# ---------------------------------------------------------------------------------------------- comparison


def test_compare_within_one_run_reports_incomplete_runs_and_missing_results(settings) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            await register(client, companies)
            job = await make_job(client)
            other_job = await make_job(client)
            files = {"a": "01_strong_match.pdf", "b": "05_intern.txt", "blocked": "06_missing_required.txt"}
            resume_ids = {}
            for key, name in files.items():
                target = await upload(svc, client, name)
                r = await client.post("/v1/resumes/register", json=item(target, job, consent=key != "blocked"))
                assert r.status_code == 201, r.text
                resume_ids[key] = r.json()["resume"]["id"]
            target = await upload(svc, client, "07_location_notice.txt")
            other = (await client.post("/v1/resumes/register", json=item(target, other_job))).json()["resume"]["id"]
            await drain(svc)
            cand = {k: (await client.get(f"/v1/resumes/{rid}")).json()["candidate_id"] for k, rid in resume_ids.items()}
            cand["other"] = (await client.get(f"/v1/resumes/{other}")).json()["candidate_id"]

            run = await client.post(f"/v1/jobs/{job}/screening-runs")
            assert run.status_code == 202, run.text
            run_id = run.json()["id"]
            assert run.json()["total"] == 2 and len(run.json()["blocked"]) == 1
            assert await Worker(svc).run_once()  # one of the two applications is processed

            ids = [cand["a"], cand["b"], cand["blocked"], cand["other"]]
            r = await client.post("/v1/candidates/compare", json={"job_id": job, "candidate_ids": ids})
            assert r.status_code == 200, r.text
            partial = r.json()
            assert partial["run_id"] == run_id and partial["run_complete"] is False
            assert (partial["done"], partial["total"]) == (1, 2)
            rows = {row["candidate_id"]: row for row in partial["candidates"]}
            assert {rows[cand["a"]]["status"], rows[cand["b"]]["status"]} == {"scored", "pending"}
            pending = next(row for row in rows.values() if row["status"] == "pending")
            assert pending["score"] is None and pending["features"] == {} and pending["screened"] is False
            assert rows[cand["blocked"]]["status"] == "blocked" and "consent" in rows[cand["blocked"]]["reason"]
            assert rows[cand["other"]]["status"] == "not_in_run" and rows[cand["other"]]["score"] is None

            await drain(svc)
            full = (await client.post("/v1/candidates/compare", json={"job_id": job, "candidate_ids": ids[:2]})).json()
            assert full["run_complete"] is True
            assert [row["status"] for row in full["candidates"]] == ["scored", "scored"]
            assert all(0 <= row["score"] <= 100 and row["features"] for row in full["candidates"])

            # An explicit run_id compares within that run, even after a newer run exists.
            assert (await client.post(f"/v1/jobs/{job}/screening-runs")).status_code == 202
            pinned = await client.post("/v1/candidates/compare",
                                       json={"job_id": job, "candidate_ids": ids[:2], "run_id": run_id})  # fmt: skip
            assert pinned.json()["run_id"] == run_id and pinned.json()["run_complete"] is True
            elsewhere = {"job_id": other_job, "candidate_ids": ids[:2], "run_id": run_id}
            wrong_job = await client.post("/v1/candidates/compare", json=elsewhere)
            assert wrong_job.status_code == 404
            no_runs = await client.post("/v1/candidates/compare", json={**elsewhere, "run_id": None})
            assert no_runs.status_code == 404
            for bad in ([cand["a"]], [cand["a"], cand["a"]], [str(uuid.uuid4()) for _ in range(6)]):
                bad_r = await client.post("/v1/candidates/compare", json={"job_id": job, "candidate_ids": bad})
                assert bad_r.status_code == 422

    asyncio.run(scenario())


# ---------------------------------------------------------------------------------------------- consent and privacy


def test_consent_lifecycle_gates_screening(settings) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            admin = await register(client, companies)
            manager = await add_user(client, admin, "hiring_manager")
            job_id, _, _, candidate_id = await prepare_run(svc, client)
            await drain(svc)
            consents = (await client.get(f"/v1/candidates/{candidate_id}/consents", headers=manager)).json()
            assert consents["has_active_consent"] is True and len(consents["consents"]) == 1
            first = consents["consents"][0]
            assert first["active"] and first["source"] == "integration form" and first["consent_id"]

            future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
            for bad in ({"source": "form", "purpose": "p", "granted_at": future},
                        {"source": "form", "purpose": "screening", "granted_at": "2026-01-01T00:00:00"},
                        {"source": "f", "purpose": "screening"}):  # fmt: skip
                assert (await client.post(f"/v1/candidates/{candidate_id}/consents", json=bad)).status_code == 422

            revoked = await client.post(f"/v1/candidates/{candidate_id}/consents/revoke")
            assert revoked.status_code == 200 and revoked.json()["revoked"] == 1
            listed = (await client.get(f"/v1/candidates/{candidate_id}/consents")).json()
            assert listed["has_active_consent"] is False and listed["consents"][0]["revoked_at"]
            blocked = await client.post(f"/v1/jobs/{job_id}/screening-runs")
            assert blocked.status_code == 409 and "consent" in blocked.text

            granted = await client.post(f"/v1/candidates/{candidate_id}/consents",
                                        json={"source": "email reply", "purpose": "recruitment screening"})  # fmt: skip
            assert granted.status_code == 201
            listed = (await client.get(f"/v1/candidates/{candidate_id}/consents")).json()
            assert listed["has_active_consent"] is True and len(listed["consents"]) == 2
            assert listed["consents"][0]["source"] == "email reply"  # newest first
            assert (await client.post(f"/v1/jobs/{job_id}/screening-runs")).status_code == 202

    asyncio.run(scenario())


async def _rows_referencing(svc, ids: list[str]) -> dict[str, int]:
    """Rows in any platform table whose candidate/application/resume id column holds one of ``ids``."""
    found: dict[str, int] = {}
    async with svc.db.system_session() as s:
        for t in Base.metadata.sorted_tables:
            for col in ("candidate_id", "application_id", "resume_id"):
                if col in t.columns:
                    n = (
                        await s.execute(
                            select(text("count(*)"))
                            .select_from(table(t.name, column(col)))
                            .where(text(f"CAST({col} AS text) = ANY(:ids)")),
                            {"ids": ids},
                        )
                    ).scalar_one()
                    if n:
                        found[f"{t.name}.{col}"] = n
    return found


def test_export_is_complete_and_erasure_leaves_nothing(settings) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            admin = await register(client, companies)
            ids = await screened_application(svc, client)
            cid, app_id = ids["candidate_id"], ids["application_id"]
            await client.post(f"/v1/applications/{app_id}/decision", json={"decision": "shortlist", "reason": "Fit"})

            export = await client.get(f"/v1/candidates/{cid}/export")
            assert export.status_code == 200
            data = export.json()
            original = (SAMPLE_DIR / "resumes" / "01_strong_match.pdf").read_bytes()
            assert data["contact"]["email"] == EMAIL and "email_hash" not in data["contact"]
            assert base64.b64decode(data["resume_files"][0]["content_base64"]) == original
            assert [d["new_stage"] for d in data["decisions"]] == ["shortlisted"]
            assert data["consents"] and data["applications"] and data["screening_results"] and data["screening_inputs"]
            assert data["extractions"] and data["registrations"] and data["audit_log"]
            assert EMAIL not in json.dumps(data["audit_log"]).lower()

            async with svc.db.system_session() as s:
                resume = (await s.execute(select(Resume).where(Resume.id == uuid.UUID(ids["resume_id"])))).scalar_one()
            assert await svc.storage.exists(resume.blob_name)
            deleted = await client.delete(f"/v1/candidates/{cid}")
            assert deleted.status_code == 200, deleted.text
            assert deleted.json()["resumes"] == 1 and deleted.json()["applications"] == 1

            assert await _rows_referencing(svc, [cid, app_id, ids["resume_id"]]) == {}
            assert not await svc.storage.exists(resume.blob_name)
            company_id = uuid.UUID(admin["company_id"])
            async with svc.db.system_session() as s:
                cache = await s.execute(
                    select(ExtractionCacheEntry).where(ExtractionCacheEntry.content_hash == resume.content_hash)
                )
                assert cache.first() is None
                jobs = (await s.execute(select(QueueJob.payload).where(QueueJob.company_id == company_id))).scalars()
                assert not [p for p in jobs if {cid, app_id, ids["resume_id"]} & set(map(str, p.values()))]
                audits = (await s.execute(select(AuditLog).where(AuditLog.company_id == company_id))).scalars().all()
                tombstone = [a for a in audits if a.action == "candidate.deleted"]
                assert len(tombstone) == 1 and tombstone[0].after["erasure_tombstones"] == 2
                dumped = json.dumps([[a.before, a.after] for a in audits]).lower()
                assert EMAIL not in dumped and "priya" not in dumped and "90000" not in dumped
                stones = (
                    (await s.execute(select(ErasureTombstone).where(ErasureTombstone.company_id == company_id)))
                    .scalars()
                    .all()
                )
                assert {t.kind for t in stones} == {"email", "phone"}
                assert all(len(t.contact_hash) == 64 and EMAIL not in t.contact_hash for t in stones)

            assert (await client.get(f"/v1/candidates/{cid}")).status_code == 404
            assert (await client.delete(f"/v1/candidates/{cid}")).status_code == 404
            assert (await client.get(f"/v1/candidates/{cid}/export")).status_code == 404

    asyncio.run(scenario())


def test_upload_registered_before_erasure_does_not_recreate_the_candidate(settings) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            await register(client, companies)
            job_id, _, _, candidate_id = await prepare_run(svc, client)
            await drain(svc)
            # A second resume of the same person is registered, but its ingestion has not run yet.
            pending = await upload(svc, client, "priya_v2.txt", SAME_PERSON_V2)
            r = await client.post("/v1/resumes/register", json=item(pending, job_id))
            assert r.status_code == 201
            pending_resume = r.json()["resume"]["id"]
            assert (await client.delete(f"/v1/candidates/{candidate_id}")).status_code == 200

            await drain(svc)  # ingestion runs after the erasure
            assert (await client.get(f"/v1/resumes/{pending_resume}")).status_code == 404
            assert not await svc.storage.exists(pending["blob_name"])
            assert (await client.get("/v1/candidates", params={"email": EMAIL})).json() == []
            async with svc.db.system_session() as s:
                company = uuid.UUID(companies[-1].hex)
                count = (await s.execute(select(text("count(*)")).select_from(Candidate).where(
                    Candidate.company_id == company))).scalar_one()  # fmt: skip
                assert count == 0
                actions = (
                    (await s.execute(select(AuditLog.action).where(AuditLog.company_id == company))).scalars().all()
                )
                assert "resume.discarded_after_erasure" in actions

            # A new upload after the erasure is a new submission and is processed normally.
            fresh = await upload(svc, client, "priya_v3.txt", SAME_PERSON_V2 + b"\nUpdated after erasure\n")
            r = await client.post("/v1/resumes/register", json=item(fresh, job_id))
            assert r.status_code == 201
            await drain(svc)
            resumed = (await client.get(f"/v1/resumes/{r.json()['resume']['id']}")).json()
            assert resumed["status"] == "ready" and resumed["candidate_id"] not in (None, candidate_id)

    asyncio.run(scenario())


def test_erasure_during_screening_retry_and_in_flight_job_recreates_nothing(settings, monkeypatch) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            await register(client, companies)
            # 1) A screening job fails once and waits for its retry; the candidate is erased meanwhile.
            _, run_id, _, candidate_id = await prepare_run(svc, client)
            monkeypatch.setattr(screening, "assess", lambda *a: (_ for _ in ()).throw(RuntimeError("transient")))
            assert await Worker(svc).run_once()
            async with svc.db.system_session() as s:
                assert (await s.execute(select(QueueJob.status).where(QueueJob.kind == "screen_application",
                        QueueJob.company_id == companies[-1]))).scalar_one() == "queued"  # fmt: skip
            assert (await client.delete(f"/v1/candidates/{candidate_id}")).status_code == 200
            monkeypatch.undo()
            async with svc.db.system_session() as s:
                await s.execute(update(QueueJob).values(run_after=datetime.now(UTC)))
                retries = await s.execute(
                    select(QueueJob.id).where(
                        QueueJob.kind == "screen_application", QueueJob.company_id == companies[-1]
                    )
                )
                assert retries.first() is None  # the pending retry was removed with the candidate
            await drain(svc)  # remaining background work (talent-pool matching) recreates nothing
            run = (await client.get(f"/v1/screening-runs/{run_id}")).json()
            assert run["status"] == "completed" and run["total"] == run["done"] == 0
            assert await _rows_referencing(svc, [candidate_id]) == {}

            # 2) A screening job is mid-assessment when the candidate is erased.
            _, run_id, resume_id, candidate_id = await prepare_run(svc, client)
            async with svc.db.system_session() as s:
                content_hash = (await s.execute(select(Resume.content_hash).where(
                    Resume.id == uuid.UUID(resume_id)))).scalar_one()  # fmt: skip
            started, release = threading.Event(), threading.Event()
            original = screening.assess

            def paused(*args):
                started.set()
                assert release.wait(timeout=10)
                return original(*args)

            monkeypatch.setattr(screening, "assess", paused)
            work = asyncio.create_task(Worker(svc).run_once())
            try:
                assert await asyncio.to_thread(started.wait, 10)
                assert (await client.delete(f"/v1/candidates/{candidate_id}")).status_code == 200
            finally:
                release.set()
                assert await work  # finishes as a no-op: the candidate no longer exists
            assert await _rows_referencing(svc, [candidate_id, resume_id]) == {}
            async with svc.db.system_session() as s:
                for model, where in ((ExtractionCacheEntry, ExtractionCacheEntry.content_hash == content_hash),
                                     (ScreeningResult, ScreeningResult.run_id == uuid.UUID(run_id)),
                                     (ScreeningInput, ScreeningInput.run_id == uuid.UUID(run_id))):  # fmt: skip
                    assert (await s.execute(select(model).where(where))).first() is None, model.__name__
                statuses = (await s.execute(select(QueueJob.status).where(QueueJob.company_id == companies[-1],
                            QueueJob.kind == "screen_application"))).scalars().all()  # fmt: skip
                assert "queued" not in statuses and "running" not in statuses

    asyncio.run(scenario())


# ---------------------------------------------------------------------------------------------- retention


def test_retention_skips_candidates_active_since_the_sweep(settings) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            admin = await register(client, companies)
            company = uuid.UUID(admin["company_id"])
            _, _, _, candidate_id = await prepare_run(svc, client)
            await drain(svc)
            cid = uuid.UUID(candidate_id)

            async def set_activity(when: datetime) -> None:
                async with svc.db.system_session() as s:
                    await s.execute(update(Candidate).where(Candidate.id == cid).values(last_activity_at=when))

            await set_activity(datetime.now(UTC) - timedelta(days=200))
            async with svc.db.system_session() as s:  # one expired and one live erasure tombstone
                s.add(ErasureTombstone(company_id=company, kind="email", contact_hash="a" * 64,
                                       expires_at=datetime.now(UTC) - timedelta(days=1)))  # fmt: skip
                s.add(ErasureTombstone(company_id=company, kind="email", contact_hash="b" * 64,
                                       expires_at=datetime.now(UTC) + timedelta(days=1)))  # fmt: skip
            now = datetime.now(UTC).replace(hour=settings.retention_sweep_hour_utc)
            await schedule_retention(svc, now=now)
            assert await Worker(svc).run_once()  # the sweep queues deletion
            async with svc.db.system_session() as s:
                payload = (await s.execute(select(QueueJob.payload).where(QueueJob.kind == "delete_candidate",
                           QueueJob.company_id == company))).scalar_one()  # fmt: skip
                assert payload["inactive_before"]
                stones = (await s.execute(select(ErasureTombstone.contact_hash).where(
                    ErasureTombstone.company_id == company))).scalars().all()  # fmt: skip
                assert stones == ["b" * 64]  # expired tombstones are purged by the sweep

            await set_activity(datetime.now(UTC))  # the candidate re-applies before the deletion job runs
            assert await Worker(svc).run_once()
            assert (await client.get(f"/v1/candidates/{candidate_id}")).status_code == 200
            async with svc.db.system_session() as s:
                actions = (await s.execute(select(AuditLog.action).where(AuditLog.company_id == company))).scalars()
                assert "candidate.retention_skipped" in list(actions)

            # Once inactive again, a later sweep selects and deletes them.
            await set_activity(datetime.now(UTC) - timedelta(days=200))
            await schedule_retention(svc, now=now + timedelta(days=1))
            assert await drain(svc) == 2
            assert (await client.get(f"/v1/candidates/{candidate_id}")).status_code == 404

    asyncio.run(scenario())


# ---------------------------------------------------------------------------------------------- dashboard APIs


def test_retention_settings_and_usage_dashboard_apis(settings) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            admin = await register(client, companies)
            company = uuid.UUID(admin["company_id"])
            recruiter = await add_user(client, admin, "recruiter")
            settings_r = await client.get("/v1/company")
            assert settings_r.status_code == 200
            assert settings_r.json() == {
                "id": admin["company_id"], "name": "Phase 2 integration test", "retention_days": 180,
                "retention_sweep_hour_utc": settings.retention_sweep_hour_utc,
                "candidates_total": 0, "candidates_past_retention": 0,
            }  # fmt: skip
            async with svc.db.tenant_session(company) as s:
                s.add(Candidate(id=uuid.uuid4(), company_id=company,
                                last_activity_at=datetime.now(UTC) - timedelta(days=40)))  # fmt: skip
            for bad in (0, 3651, "soon"):
                assert (await client.patch("/v1/company", json={"retention_days": bad})).status_code == 422
            updated = await client.patch("/v1/company", json={"retention_days": 30})
            assert updated.status_code == 200
            assert updated.json()["retention_days"] == 30 and updated.json()["candidates_past_retention"] == 1
            assert (await client.get("/v1/company")).json()["retention_days"] == 30
            assert (await client.get("/v1/company", headers=recruiter)).status_code == 403

            async with svc.db.system_session() as s:
                for day, tokens, pages in ((1, 100, 0), (1, 50, 2), (3, 25, 0)):
                    s.add(UsageEvent(company_id=company, kind="ocr" if pages else "llm", prompt_tokens=tokens,
                                     ocr_pages=pages, cost_usd=tokens / 1000,
                                     created_at=datetime(2026, 9, day, 12, tzinfo=UTC)))  # fmt: skip
            usage = (await client.get("/v1/usage")).json()
            assert usage["totals"]["prompt_tokens"] == 175 and usage["totals"]["ocr_pages"] == 2
            assert usage["by_day"] == [
                {"date": "2026-09-01", "prompt_tokens": 150, "completion_tokens": 0, "ocr_pages": 2, "cost_usd": 0.15},
                {"date": "2026-09-03", "prompt_tokens": 25, "completion_tokens": 0, "ocr_pages": 0, "cost_usd": 0.025},
            ]
            assert set(usage["by_kind"]) == {"llm", "ocr"}
            ranged = (await client.get("/v1/usage", params={"from": "2026-09-02", "to": "2026-09-04"})).json()
            assert ranged["totals"]["prompt_tokens"] == 25 and ranged["from"].startswith("2026-09-02T00:00:00")
            naive = (await client.get("/v1/usage", params={"from": "2026-09-01T00:00:00"})).json()
            assert datetime.fromisoformat(naive["from"]) == datetime(2026, 9, 1, tzinfo=UTC)
            for params in ({"from": "2026-09-05", "to": "2026-09-01"}, {"from": "2026-09-01", "to": "2026-09-01"},
                           {"from": "not-a-date"}, {"to": "2026-13-01"}):  # fmt: skip
                assert (await client.get("/v1/usage", params=params)).status_code == 422, params
            assert (await client.get("/v1/usage", headers=recruiter)).status_code == 403

    asyncio.run(scenario())
