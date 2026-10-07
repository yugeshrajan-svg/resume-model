import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from tests.test_backend_integration import platform, prepare_run, register
from tests.test_backend_integration import settings as settings
from tests.test_hr_management import add_user

pytestmark = pytest.mark.integration


def test_recruiting_persists_pipeline_activities_feedback_reports_and_company_isolation(settings):
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            admin = await register(client, companies)
            original_headers = dict(client.headers)
            job_id, _, _, candidate_id = await prepare_run(svc, client)
            pipeline = (await client.get(f"/v1/jobs/{job_id}/pipeline")).json()
            assert pipeline["total"] == 1
            app = pipeline["items"][0]
            for term in ["01_strong_match", candidate_id[:8], app["name"] or "01_strong_match"]:
                found = await client.get(f"/v1/jobs/{job_id}/pipeline", params={"q": term})
                assert found.status_code == 200, found.text
                assert found.json()["total"] == 1
            for term in ["no matching candidate", "%", "_"]:
                # Search terms are literal substrings rather than SQL wildcard patterns.
                expected = 1 if term == "_" else 0
                assert (await client.get(f"/v1/jobs/{job_id}/pipeline", params={"q": term})).json()["total"] == expected
            path = f"/v1/applications/{app['id']}/stage"
            move = await client.patch(
                path, json={"stage": "interview", "expected_stage": "applied", "reason": "Recruiter approved interview"}
            )
            assert move.status_code == 200, move.text
            assert (
                await client.patch(path, json={"stage": "offer", "expected_stage": "applied", "reason": "Stale edit"})
            ).status_code == 409
            history = (await client.get(f"/v1/applications/{app['id']}/decisions")).json()["decisions"]
            assert history[0]["new_stage"] == "interview" and history[0]["previous_stage"] == "applied"
            start = datetime.now(UTC) + timedelta(days=1)
            base = {"candidate_id": candidate_id, "application_id": app["id"], "job_id": job_id}
            records = {}
            for kind, extra in [
                ("task", {"due_at": (start - timedelta(days=2)).isoformat()}),
                (
                    "interview",
                    {
                        "due_at": start.isoformat(),
                        "end_at": (start + timedelta(hours=1)).isoformat(),
                        "location": "Office",
                    },
                ),
                ("note", {"notes": "Recruiter call notes"}),
                (
                    "offer",
                    {
                        "amount": "1500000.00",
                        "currency": "INR",
                        "pay_period": "year",
                        "start_date": start.date().isoformat(),
                    },
                ),
            ]:
                response = await client.post(
                    "/v1/recruiting/activities", json={**base, "kind": kind, "title": f"Test {kind}", **extra}
                )
                assert response.status_code == 201, response.text
                records[kind] = response.json()["id"]
            overdue = (await client.get("/v1/recruiting/activities", params={"overdue": "true"})).json()
            assert overdue["total"] == 1 and overdue["items"][0]["id"] == records["task"]
            assert overdue["items"][0]["job_title"] == "ML Engineer"
            owner_id = overdue["items"][0]["assigned_to"]
            assert (
                await client.get("/v1/recruiting/activities", params={"assigned_to": owner_id, "status": "open"})
            ).json()["total"] == 3
            assert (await client.get("/v1/recruiting/activities", params={"q": "Test interview"})).json()["total"] == 1
            assert (await client.get("/v1/recruiting/activities", params={"q": "%"})).json()["total"] == 0
            rows = {r["kind"]: r for r in (await client.get("/v1/recruiting/activities")).json()["items"]}
            task_edit = {
                "expected_updated_at": rows["task"]["updated_at"],
                "title": "Follow up with panel",
                "notes": "Awaiting feedback",
                "due_at": None,
            }
            changed = await client.patch(f"/v1/recruiting/activities/{records['task']}/details", json=task_edit)
            assert changed.status_code == 200, changed.text
            assert changed.json()["due_at"] is None
            assert (
                await client.patch(f"/v1/recruiting/activities/{records['task']}/details", json=task_edit)
            ).status_code == 409
            assert (
                await client.patch(
                    f"/v1/recruiting/activities/{records['task']}",
                    json={"status": "completed", "expected_updated_at": rows["task"]["updated_at"]},
                )
            ).status_code == 409
            invalid_edit = {
                "expected_updated_at": rows["interview"]["updated_at"],
                "title": "Technical round",
                "notes": "",
                "end_at": (start - timedelta(hours=1)).isoformat(),
            }
            assert (
                await client.patch(f"/v1/recruiting/activities/{records['interview']}/details", json=invalid_edit)
            ).status_code == 422
            next_start = start + timedelta(hours=2)
            next_round = await client.post(
                "/v1/recruiting/activities",
                json={
                    **base,
                    "kind": "interview",
                    "title": "Next round",
                    "due_at": next_start.isoformat(),
                    "end_at": (next_start + timedelta(hours=1)).isoformat(),
                },
            )
            assert next_round.status_code == 201, next_round.text
            interview_edit = {
                "expected_updated_at": rows["interview"]["updated_at"],
                "title": "Technical round revised",
                "notes": "",
                "due_at": next_start.isoformat(),
                "end_at": (next_start + timedelta(hours=1)).isoformat(),
            }
            assert (
                await client.patch(f"/v1/recruiting/activities/{records['interview']}/details", json=interview_edit)
            ).status_code == 409
            interview_edit.update(due_at=start.isoformat(), end_at=(start + timedelta(hours=1)).isoformat())
            saved_interview = await client.patch(
                f"/v1/recruiting/activities/{records['interview']}/details", json=interview_edit
            )
            assert saved_interview.status_code == 200, saved_interview.text
            cancelled = await client.patch(
                f"/v1/recruiting/activities/{next_round.json()['id']}", json={"status": "cancelled"}
            )
            cancelled_edit = {
                "expected_updated_at": cancelled.json()["updated_at"],
                "title": "Cancelled next round",
                "notes": "",
                "due_at": start.isoformat(),
                "end_at": (start + timedelta(hours=1)).isoformat(),
            }
            revised_cancelled = await client.patch(
                f"/v1/recruiting/activities/{next_round.json()['id']}/details", json=cancelled_edit
            )
            assert revised_cancelled.status_code == 200, revised_cancelled.text
            assert (
                await client.patch(
                    f"/v1/recruiting/activities/{next_round.json()['id']}",
                    json={"status": "open", "expected_updated_at": revised_cancelled.json()["updated_at"]},
                )
            ).status_code == 409
            assert (
                "Technical round revised"
                in (await client.get(f"/v1/recruiting/activities/{records['interview']}/calendar")).text
            )
            assert (
                await client.patch(
                    f"/v1/recruiting/activities/{records['note']}/details",
                    json={
                        "expected_updated_at": rows["note"]["updated_at"],
                        "title": "Changed note",
                        "notes": "Cannot overwrite",
                    },
                )
            ).status_code == 422
            offer_edit = {
                "expected_updated_at": rows["offer"]["updated_at"],
                "title": "Revised compensation",
                "notes": "",
                "amount": "1600000.00",
            }
            updated_offer = await client.patch(f"/v1/recruiting/activities/{records['offer']}/details", json=offer_edit)
            assert updated_offer.status_code == 200, updated_offer.text
            assert updated_offer.json()["details"]["amount"] == "1600000.00"
            assert (
                await client.post(
                    "/v1/recruiting/activities",
                    json={
                        **base,
                        "kind": "interview",
                        "title": "Overlapping interview",
                        "due_at": start.isoformat(),
                        "end_at": (start + timedelta(minutes=30)).isoformat(),
                    },
                )
            ).status_code == 409
            export = await client.get(f"/v1/recruiting/activities/{records['interview']}/calendar")
            assert export.status_code == 200 and "BEGIN:VEVENT" in export.text
            manager = await add_user(client, admin, "hiring_manager")
            assert (
                await client.patch(
                    f"/v1/recruiting/activities/{records['offer']}/details", headers=manager, json=offer_edit
                )
            ).status_code == 403
            assert (
                await client.get(f"/v1/jobs/{job_id}/pipeline", headers=manager, params={"q": "01_strong_match"})
            ).json()["total"] == 0
            assert (
                await client.get(f"/v1/jobs/{job_id}/pipeline", headers=manager, params={"q": candidate_id[:8]})
            ).json()["total"] == 1
            assert (
                await client.patch(path, headers=manager, json={"stage": "hired", "reason": "Unauthorized"})
            ).status_code == 403
            assert (
                await client.post("/v1/recruiting/activities", headers=manager, json={"kind": "task", "title": "Task"})
            ).status_code == 403
            feedback = await client.post(
                "/v1/recruiting/feedback",
                headers=manager,
                json={
                    **base,
                    "kind": "feedback",
                    "title": "Technical feedback",
                    "notes": "Answered system design questions clearly",
                    "technical": 4,
                    "communication": 5,
                    "experience": 4,
                    "recommendation": "hire",
                },
            )
            assert feedback.status_code == 201, feedback.text
            hidden = (await client.get("/v1/recruiting/activities", headers=manager)).json()
            assert all(item["kind"] != "offer" for item in hidden["items"])
            assert all(item["candidate_name"] is None for item in hidden["items"])
            assert (await client.get(f"/v1/jobs/{job_id}/pipeline", headers=manager)).json()["items"][0]["name"] is None
            for kind, status in [("task", "completed"), ("offer", "accepted")]:
                response = await client.patch(f"/v1/recruiting/activities/{records[kind]}", json={"status": status})
                assert response.status_code == 200, response.text
            assert (
                await client.patch(f"/v1/recruiting/activities/{records['note']}", json={"status": "cancelled"})
            ).status_code == 422
            assert (
                await client.patch(
                    path,
                    json={
                        "stage": "hired",
                        "expected_stage": "interview",
                        "reason": "Offer accepted and start confirmed",
                    },
                )
            ).status_code == 200
            report = (await client.get("/v1/recruiting/reports")).json()
            assert report["stages"]["hired"] == 1 and report["hires_last_30_days"] == 1
            assert report["monthly_cohorts"][0]["applications"] == 1
            assert report["monthly_cohorts"][0]["hired"] == 1
            assert (await client.get("/v1/recruiting/reports", params={"to_date": "9999-12-31"})).status_code == 200
            assert report["average_days_to_hire"] is not None and report["overdue_tasks"] == 0
            candidate_export = (await client.get(f"/v1/candidates/{candidate_id}/export")).json()
            assert len(candidate_export["recruiting_activities"]) == 6
            await register(client, companies)
            assert (await client.get("/v1/recruiting/activities")).json()["total"] == 0
            assert (await client.get(f"/v1/jobs/{job_id}/pipeline")).status_code == 404
            assert (
                await client.patch(f"/v1/recruiting/activities/{records['task']}", json={"status": "open"})
            ).status_code == 404
            assert (await client.get("/v1/recruiting/reports")).json()["applications"] == 0
            assert (
                await client.patch(f"/v1/recruiting/activities/{records['offer']}/details", json=offer_edit)
            ).status_code == 404
            # Cross-company foreign keys are rejected before insertion, even though their UUIDs are valid.
            assert (
                await client.post(
                    "/v1/recruiting/activities", json={**base, "kind": "task", "title": "Cannot attach other company"}
                )
            ).status_code == 404
            client.headers.update(original_headers)
            assert (await client.delete(f"/v1/candidates/{candidate_id}")).status_code == 200
            assert (await client.get("/v1/recruiting/activities")).json()["total"] == 0

    asyncio.run(scenario())
