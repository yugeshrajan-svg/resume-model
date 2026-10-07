# Shortlist dashboard · Phases 4–5

React, TypeScript and Vite dashboard for the persistent `backend.main` API.
Runtime dependencies and development tools are pinned in `package-lock.json`.
Fonts are bundled locally. The application uses real API data; mock data is confined to tests.

## Start locally

Use Node.js 22.12+ and npm. First complete the PostgreSQL/Azurite setup and migrations
in the [project README](../README.md), then start the platform API and worker in separate terminals:

```powershell
# From the project root
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

```powershell
# From the project root, in a second terminal
.\.venv\Scripts\python.exe -m backend.worker
```

```powershell
# From the project root, in a third terminal
cd frontend
npm ci
npm run dev
```

Open **http://127.0.0.1:5173**. Create a workspace on the sign-up page, or sign in
with an existing company account. No demo credentials are built into the dashboard.
On PowerShell, use `npm.cmd` if your execution policy blocks the `npm.ps1` shim.

Defaults match the project setup: API on port 8000 and Azurite on port 10000.
For different ports, copy `.env.example` to `.env.local` and adjust the two public addresses.
Never place backend credentials or Azure keys in a `VITE_` variable; Vite exposes those values to the browser.

Vite proxies `/v1` to the backend. In development, uploads to the exact configured
Azurite origin use a `/blob-upload` proxy, preserving the SAS path and query.
Other storage origins receive direct uploads. The frontend sends only the SAS-provided
headers to storage; it never sends the workspace bearer token to the blob service.

## Recruiter workflow

The workspace follows three steps: **Requirements → Resumes → Screening & results**.
Jobs can be searched or sorted, and creation opens in a focused dialog. Confirmed requirements
have a **Continue to resumes** action. **Filters** reveals the screening filters; active filters
remain visible in the count and can be cleared even when the controls are closed. **Run details**
contains the reference date, requirements version and failed-candidate retry action.

Candidate reviews show strengths and gaps first, with the decision form alongside on desktop
and immediately after the evidence on mobile. Expand **Feature breakdown** for individual
scores and supporting quotes. The hiring guide explains the same three-step workflow.

1. Create a job with its description. Analyze it or enter requirements manually.
2. Review skills, alternatives, experience, location, notice period and optional score weights.
   Edits create a draft version; confirm the intended version before screening.
3. Upload **PDF, DOCX or TXT resumes**, including selections larger than 500 files. Record consent only
   when every selected candidate has actually granted it, and specify its source and purpose.
4. Watch per-file processing. Duplicate resumes are linked to the job. Registration
   retries reuse the original blob; failed parsing/OCR can be reprocessed separately.
   The browser uploads three files at a time and displays 25 rows per page, with overall
   registration progress. Keep the browser tab open until uploads finish; registered files
   continue processing in the durable backend queue. Large selections and files added through multiple
   selections stay in the upload list and are processed automatically in bounded batches.
5. Start screening, watch progress, select previous runs, and filter paginated results
   by eligibility, application stage and minimum score.
   Admins and recruiters can select **Stop screening** beside an active run's progress.
   It cancels that run's remaining queue and keeps completed results, including after reload.
   An AI request already underway may finish, but it cannot append results to the stopped run.
   Start a new screening to process more candidates. The API is `POST /v1/screening-runs/{run_id}/stop`;
   restart existing worker processes after updating to enable their stop checkpoint guards.
6. Open a candidate for contact details when permitted, resume history, the assessed
   resume version, per-feature evidence, strengths and gaps.
7. Select 2–5 candidates to compare within the selected screening run. The full run roster
   includes pending and excluded candidates; comparison polls while the run is incomplete.
8. Record shortlist, reject or further-review decisions with an optional reason. Review
   the append-only history, including stage changes, reviewer and timestamp.
9. Review consent history. Admins/recruiters can record a real grant or revoke all active grants.
   Failed resumes can be reprocessed from their profile or the upload list.

Admins and recruiters can create jobs, edit requirements, upload and screen. Hiring managers
can review jobs/results, compare candidates and record decisions. Candidate contacts and resume
filenames are restricted to admins and recruiters; hiring managers see candidate IDs and a hidden-filename label.
All operations still rely on the backend's authorization and tenant isolation.

Admins also have **Team**, **Retention & settings** and **Usage & cost** screens. Team accounts
can be created with an explicit role and initial password. The current backend supports listing
and creation, so the dashboard offers those account operations.

Candidate exports download directly as JSON, including original source files, without keeping
the export in query or browser storage. Deletion requires confirmation, removes the candidate
from every job, clears related query data and upload manifests in this tab, and returns to results.
Service errors keep the confirmation open for an explicit retry.

Shortening retention requires confirmation with the count past the currently saved retention
period. The sweep hour is read-only, in UTC. Saving changes the next daily sweep; it does not
delete candidates immediately. Usage filters use an inclusive UTC start and an exclusive UTC
end. Costs retain six decimal places. Daily charts fill missing days with zero for bounded
periods up to 366 days; other periods show recorded dates. Exact values remain in the tables.

The upload list is scoped to the company/job and saved in this browser tab's session storage.
Registered resumes and results live in the backend. On reload, registered file status is
fetched again; uploaded blobs awaiting registration can be retried without another PUT.
Files that have not finished uploading must be selected again after reload.
Upload progress continues when changing dashboard tabs, and sign-out cancels pending uploads
and clears local upload history and query caches. The current API has no job-wide resume-list
endpoint, so another browser sees registered candidates through screening results rather than
the original upload list.

Access and rotating refresh tokens use session storage, with an in-memory fallback if storage
is blocked. Concurrent requests share one refresh. Invalid refresh credentials return to sign-in;
temporary service failures show retry controls. Sign-out clears the local session; the existing
backend does not provide a token-revocation/logout endpoint.

## Validation

```powershell
npm run build
npm run lint
npm run format:check
npm test
npx playwright install chromium
npm run test:e2e
```

Unit tests cover concurrent refresh, session changes, registration recovery, erasure of cached
upload entries across jobs, and UTC usage boundaries.
Browser tests cover job creation, requirements editing/confirmation, evidence review, permissions,
decisions/history, comparison polling, consent, JSON export, deletion retries, team creation,
retention confirmation, usage precision, API errors, mobile navigation and automated WCAG checks.
They use mocked API responses and need
no database or cloud credentials.

With a real local API, worker, PostgreSQL and Azurite running:

```powershell
npm run test:e2e:live
```

Use a disposable development database and storage container for live tests. They create synthetic
company accounts and candidate files. The tests verify mixed bulk uploads, content deduplication,
processing, screening, evidence, reload recovery, missing-consent exclusions, team and retention
settings, comparison, decisions by admins and hiring managers, consent revoke/regrant, complete
exports and candidate erasure. They also exercise PDF/DOCX uploads, the image-only synthetic OCR
fixture in `e2e/assets/scanned-resume.pdf`, talent-pool indexing/search, public applications,
interview booking, hiring plans, feedback, tasks, offers, calendar/report exports, hiring templates,
and main-screen mobile layouts. The running API/worker's extraction, embedding and OCR profile is
used; the free local AI profile needs no paid API. Processing assertions allow three minutes,
with ten minutes per workflow to accommodate local model inference. Service addresses can be
changed with the same proxy environment variables.
Test screenshots and traces are written to ignored `test-results/ui` and `test-results/live` directories.

## Production artifact

`npm run build` produces `dist/`. The [Vite guide](https://vite.dev/guide/build.html)
documents the static build. Serve it with SPA fallback to `index.html` and reverse-proxy `/v1`
to the platform API on the same origin. The development upload proxy is absent from the build;
configure Blob Storage CORS for the dashboard origin and allow PUT plus the SAS upload headers.
`npm run preview` previews the static artifact; it is not a complete production deployment.

## Phase 5 API integration

The dashboard implementation is contained in `frontend/`. HR management uses the backend contracts
in [`docs/hr-management-api.md`](../docs/hr-management-api.md). Apply migration `0003_hr_management`
with `python -m alembic upgrade head` before starting this dashboard against an existing database.
Old decisions whose previous stage is null display "Earlier stage unavailable".

The current frontend contracts are in `src/api/types.ts`; all backend calls use `src/api/client.ts`.
Existing endpoints used by the dashboard:

| Area           | Routes under `/v1`                                                                     |
| -------------- | -------------------------------------------------------------------------------------- |
| Authentication | `POST /auth/login`, `/auth/register-company`, `/auth/refresh`                          |
| Jobs           | `GET/POST /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/analyze`                          |
| Requirements   | `GET/PUT /jobs/{id}/requirements`, `POST /jobs/{id}/requirements/confirm`              |
| Uploads        | `POST /uploads/sas`, `POST /resumes/register-bulk`                                     |
| Processing     | `GET /resumes/{id}`, `POST /resumes/{id}/reprocess`                                    |
| Screening      | `GET/POST /jobs/{id}/screening-runs`, `POST /screening-runs/{id}/retry`                |
| Results        | `GET /jobs/{id}/candidates`, `GET /candidates/{id}`, `GET /screening-runs/{id}/inputs` |
| Decisions      | `POST /applications/{id}/decision`, `GET /applications/{id}/decisions`                 |
| Comparison     | `POST /candidates/compare` (always includes the selected `run_id`)                     |
| Consent        | `GET/POST /candidates/{id}/consents`, `POST /candidates/{id}/consents/revoke`          |
| Candidate data | `GET /candidates/{id}/export`, `DELETE /candidates/{id}`                               |
| Administration | `GET/POST /users`, `GET/PATCH /company`, `GET /usage`                                  |

Keep these request/response fields compatible when extending the backend. Candidate display names
reuse cached profile requests for the current results page (at most 20 rows); hiring managers use
candidate IDs. An optional contact-permission-aware summary in ranked results can later replace
those extra profile reads without changing the workflow.
