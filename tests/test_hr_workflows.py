import asyncio

import pytest

from backend.security import decode_access_token
from tests.test_backend_integration import platform, prepare_run, register
from tests.test_backend_integration import settings as settings
from tests.test_hr_management import add_user

pytestmark = pytest.mark.integration


def test_hiring_plans_approvals_letters_and_handover(settings):
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            admin = await register(client, companies)
            original = dict(client.headers)
            manager = await add_user(client, admin, "hiring_manager")
            recruiter = await add_user(client, admin, "recruiter")
            catalog = (await client.get("/v1/hr/catalog")).json()
            assert set(catalog) == {"requisition", "offer_approval", "offer_template", "onboarding"}
            for kind in ["employee", "leave", "payroll", "performance"]:
                assert (
                    await client.post("/v1/hr/records", json={"kind": kind, "title": "Excluded workflow", "data": {}})
                ).status_code == 403

            def uid(headers):
                return str(decode_access_token(headers["Authorization"].split()[1], svc.settings).user_id)

            job, _, _, candidate = await prepare_run(svc, client)
            plan = {
                "expected_version": 0,
                "rounds": [{"id": "technical", "name": "Technical round", "criteria": ["Problem solving", "Design"]}],
            }
            response = await client.put(f"/v1/jobs/{job}/hiring-plan", json=plan)
            assert response.status_code == 200, response.text
            assert (await client.put(f"/v1/jobs/{job}/hiring-plan", json=plan)).status_code == 409
            assert (await client.put(f"/v1/jobs/{job}/hiring-plan", headers=manager, json=plan)).status_code == 403
            feedback = {
                "kind": "feedback",
                "candidate_id": candidate,
                "job_id": job,
                "title": "Round evidence",
                "notes": "Solid role evidence",
                "technical": 4,
                "communication": 4,
                "experience": 4,
                "recommendation": "hire",
                "round_id": "technical",
                "plan_version": 1,
                "ratings": {"Problem solving": 4, "Design": 5},
            }
            assert (await client.post("/v1/recruiting/feedback", headers=manager, json=feedback)).status_code == 201
            assert (
                await client.post(
                    "/v1/recruiting/feedback",
                    headers=manager,
                    json={**feedback, "ratings": {"Problem solving": True, "Design": 4}},
                )
            ).status_code == 422
            assert (
                await client.post(
                    "/v1/recruiting/feedback", headers=manager, json={**feedback, "ratings": {"Design": 4}}
                )
            ).status_code == 422
            plan["expected_version"] = 1
            plan["rounds"][0]["criteria"] = ["New criterion"]
            assert (await client.put(f"/v1/jobs/{job}/hiring-plan", json=plan)).status_code == 200
            saved = (await client.get("/v1/recruiting/activities", params={"kind": "feedback"})).json()["items"][0]
            assert saved["details"]["criteria"] == ["Problem solving", "Design"]

            req = {
                "kind": "requisition",
                "title": "New engineering opening",
                "assigned_to": uid(manager),
                "data": {
                    "job_title": "Platform engineer",
                    "jd_text": "Build reliable services",
                    "headcount": "1",
                    "justification": "Approved growth plan",
                },
            }
            requested = await client.post("/v1/hr/records", json=req)
            assert requested.status_code == 201, requested.text
            decision = {
                "action": "approve",
                "expected_updated_at": requested.json()["updated_at"],
                "reason": "Budget and role reviewed",
            }
            assert (
                await client.post(f"/v1/hr/records/{requested.json()['id']}/action", json=decision)
            ).status_code == 403
            approved = await client.post(
                f"/v1/hr/records/{requested.json()['id']}/action", headers=manager, json=decision
            )
            assert approved.status_code == 200, approved.text
            assert approved.json()["job_id"] and len(approved.json()["history"]) == 1

            offer = await client.post(
                "/v1/recruiting/activities",
                json={
                    "kind": "offer",
                    "candidate_id": candidate,
                    "job_id": job,
                    "title": "Engineer offer",
                    "amount": "1500000",
                    "start_date": "2026-11-01",
                },
            )
            assert offer.status_code == 201, offer.text
            offer_id = offer.json()["id"]
            approval = await client.post(
                "/v1/hr/records",
                json={
                    "kind": "offer_approval",
                    "title": "Compensation approval",
                    "activity_id": offer_id,
                    "assigned_to": uid(recruiter),
                    "data": {"justification": "Reviewed compensation band"},
                },
            )
            assert approval.status_code == 201, approval.text
            assert (
                await client.patch(f"/v1/recruiting/activities/{offer_id}", json={"status": "accepted"})
            ).status_code == 409
            approved_offer = await client.post(
                f"/v1/hr/records/{approval.json()['id']}/action",
                headers=recruiter,
                json={
                    "action": "approve",
                    "expected_updated_at": approval.json()["updated_at"],
                    "reason": "Compensation approved",
                },
            )
            assert approved_offer.status_code == 200, approved_offer.text
            assert (
                await client.patch(f"/v1/recruiting/activities/{offer_id}", json={"status": "accepted"})
            ).status_code == 200
            template = await client.post(
                "/v1/hr/records",
                json={
                    "kind": "offer_template",
                    "title": "Standard offer",
                    "data": {
                        "content": "Dear {{candidate}},\nOffer for {{job}}: {{currency}} {{amount}}.\n"
                        "<script>never execute</script>"
                    },
                },
            )
            assert template.status_code == 201, template.text
            current_offer = (await client.get("/v1/recruiting/activities", params={"kind": "offer"})).json()["items"][0]
            letter = await client.post(
                f"/v1/recruiting/offers/{offer_id}/letter",
                json={
                    "template_id": template.json()["id"],
                    "expected_updated_at": current_offer["updated_at"],
                    "reviewed": True,
                },
            )
            assert letter.status_code == 200, letter.text
            assert "&lt;script&gt;" in letter.text and "<script>" not in letter.text and "1500000" in letter.text
            assert (
                await client.post(
                    f"/v1/recruiting/offers/{offer_id}/letter",
                    headers=manager,
                    json={
                        "template_id": template.json()["id"],
                        "expected_updated_at": current_offer["updated_at"],
                        "reviewed": True,
                    },
                )
            ).status_code == 403

            apps = (await client.get(f"/v1/jobs/{job}/pipeline")).json()["items"]
            assert (
                await client.post(
                    "/v1/hr/records",
                    json={
                        "kind": "onboarding",
                        "candidate_id": candidate,
                        "title": "New hire",
                        "data": {"start_date": "2026-11-01", "checklist": "Laptop\nOrientation"},
                    },
                )
            ).status_code == 422
            await client.patch(
                f"/v1/applications/{apps[0]['id']}/stage", json={"stage": "hired", "reason": "Start confirmed"}
            )
            handover = await client.post(
                "/v1/hr/records",
                json={
                    "kind": "onboarding",
                    "candidate_id": candidate,
                    "title": "New hire",
                    "data": {"start_date": "2026-11-01", "checklist": "Laptop\nOrientation"},
                },
            )
            assert handover.status_code == 201, handover.text
            assert (
                await client.post(
                    f"/v1/hr/records/{handover.json()['id']}/action",
                    json={
                        "action": "checklist",
                        "expected_updated_at": handover.json()["updated_at"],
                        "reason": "Ready for first day",
                        "completed_items": [0, 1],
                    },
                )
            ).json()["status"] == "completed"
            await register(client, companies)
            assert (await client.get("/v1/hr/records")).json()["total"] == 0
            client.headers.update(original)

    asyncio.run(scenario())
