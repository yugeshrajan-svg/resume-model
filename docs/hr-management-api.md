# HR management API

Backend endpoints for the HR dashboard: roles, candidate profiles, decisions and their history, comparison
within a screening run, consent, export and erasure, retention settings and usage. All paths are under the
platform API (`backend.main:app`) and require `Authorization: Bearer <access_token>` from `POST /v1/auth/login`.

Existing endpoints keep their earlier response fields; the fields listed as *added* are additive.

## Conventions

- **Tenant isolation.** Every request is scoped to the caller's company (repository filter plus PostgreSQL row-level
  security). An id from another company behaves exactly like an id that does not exist: `404 {"detail":
  "Resource not found"}`. Searches return `[]`. The dashboard cannot tell the two apart, by design.
- **Timestamps** are ISO 8601 with a timezone. UTC may be rendered as `Z` or `+00:00`; parse both.
- **Ids** are UUIDs. A malformed id in a path or body is a `422`.

### Errors

| Status | Body | When |
|---|---|---|
| 401 | `{"detail": "..."}` | Missing, expired or invalid access token. |
| 403 | `{"detail": "..."}` | The caller's role lacks the endpoint's permission. Checked before the resource is looked up. |
| 404 | `{"detail": "Resource not found"}` | Unknown id, or an id belonging to another company. |
| 404 | `{"detail": "This job has no screening runs yet"}` | Ranked list / compare without a run. |
| 409 | `{"detail": "...", "details": {...}}` | Request conflicts with current state (e.g. no screenable applications). `details` is present only when useful. |
| 422 | FastAPI validation body (`{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}`) or `{"detail": "..."}` | Invalid input. |
| 503 | `{"detail": "Database temporarily unavailable"}` / `"Storage temporarily unavailable"` | Transient infrastructure failure; safe to retry. |

## Roles and permissions

| Permission | admin | recruiter | hiring_manager |
|---|:-:|:-:|:-:|
| `candidates:read` – profiles, ranked lists, compare, consents list, decision history | ✓ | ✓ | ✓ |
| `decisions:write` – shortlist / reject / review | ✓ | ✓ | ✓ |
| `contacts:read` – name, email, phone, resume filenames, candidate search | ✓ | ✓ | |
| `consents:write` – record / revoke consent | ✓ | ✓ | |
| `screening:run`, `jobs:write`, `resumes:write` | ✓ | ✓ | |
| `candidates:export`, `candidates:delete` | ✓ | | |
| `company:manage` – retention settings | ✓ | | |
| `usage:read`, `users:manage` | ✓ | | |

**Restricted contact visibility.** For roles without `contacts:read` (hiring managers):

- `GET /v1/candidates/{id}` has no `contact` key, and every `resumes[].filename` is `null` (filenames often contain
  the candidate's name).
- `GET /v1/resumes/{id}` returns `"filename": null`.
- Ranked lists, compare, consent lists and decision history never contain contact details for any role.

Render "hidden" rather than "missing" when `contact` is absent or `filename` is `null`.

## Candidate profile

`GET /v1/candidates/{candidate_id}` — `candidates:read`

```json
{
  "candidate_id": "6c1d…",
  "created_at": "2026-10-01T09:12:44.120Z",
  "resumes": [
    {"resume_id": "a3f0…", "version": 1, "filename": "priya_raman.pdf", "status": "ready",
     "created_at": "2026-10-01T09:12:40Z"}
  ],
  "applications": [
    {"application_id": "e81b…", "job_id": "47c2…", "stage": "shortlisted",
     "decisions": [ /* same items as the decision history below, newest first */ ]}
  ],
  "consents": [ /* same items as the consent list below */ ],
  "screening": [ /* per run, newest first: run_id, job_id, requirements_version, status, score, features, explanation, … */ ],
  "contact": {"name": "Priya Raman", "email": "priya@example.com", "phone": "+91 90000 00001"}
}
```

*Added:* `applications[].decisions`; `consents[].consent_id`, `recorded_by`, `active`. `contact` is present only
with `contacts:read` (it is `null` if no contact was extracted); `filename` is `null` without it.

## Decisions

Stages: `applied` → (worker) `screened`; manual decisions set `shortlisted`, `rejected` or `review`. A decision can be
made at any stage, including before or during screening. **Screening never overwrites a manual decision**: the worker
only moves an application from `applied` to `screened`, and only if it is still on the resume that was screened. New
runs for the same job keep the current stage.

### Record a decision

`POST /v1/applications/{application_id}/decision` — `decisions:write`

```json
{"decision": "shortlist", "reason": "Strong ML platform work"}
```

`decision` is `shortlist`, `reject` or `review`; `reason` is optional (≤ 2000 characters).

`201`:

```json
{
  "decision_id": "0b9e…",
  "application_id": "e81b…",
  "stage": "shortlisted",
  "decision": "shortlist",
  "previous_stage": "screened",
  "reason": "Strong ML platform work",
  "decided_at": "2026-10-02T11:03:12.481Z",
  "reviewer": {"user_id": "5d2a…", "role": "recruiter"}
}
```

*Added:* everything after `stage`. Errors: `422` invalid `decision`/`reason`, `404` unknown application.

### Decision history

`GET /v1/applications/{application_id}/decisions` — `candidates:read`

```json
{
  "application_id": "e81b…",
  "candidate_id": "6c1d…",
  "job_id": "47c2…",
  "stage": "rejected",
  "decisions": [
    {"decision_id": "…", "decision": "reject", "previous_stage": "review", "new_stage": "rejected",
     "reason": "Position filled", "decided_at": "2026-10-03T08:00:00Z",
     "reviewer": {"user_id": "…", "email": "admin@acme.example", "role": "admin"}},
    {"decision_id": "…", "decision": "review", "previous_stage": "shortlisted", "new_stage": "review",
     "reason": "Panel interview", "decided_at": "2026-10-02T15:30:00Z",
     "reviewer": {"user_id": "…", "email": "hm@acme.example", "role": "hiring_manager"}}
  ]
}
```

Newest first. History is append-only (the application database role cannot update or delete decisions).
`reviewer` is `null` if the reviewer's user account was removed. Decisions recorded before migration `0003_hr_management` have `previous_stage: null`. Reviewer emails are
staff accounts, not candidate contact data, so they are shown to every role.

## Compare within one screening run

`POST /v1/candidates/compare` — `candidates:read`

```json
{"job_id": "47c2…", "candidate_ids": ["6c1d…", "9a7e…", "13bb…"], "run_id": "f00d…"}
```

- `candidate_ids`: 2–5 distinct ids (`422` otherwise).
- `run_id` (*added*, optional): the run to compare within; default is the job's latest run. Scores are never mixed
  across runs.

`200`:

```json
{
  "job_id": "47c2…",
  "run_id": "f00d…",
  "run_status": "running",
  "run_complete": false,
  "done": 1,
  "total": 2,
  "candidates": [
    {"candidate_id": "6c1d…", "status": "scored", "screened": true, "reason": null, "reprocess_reason": null,
     "score": 82.4, "eligible": true,
     "features": {"required_skills_coverage": 1.0, "notice_period_fit": null},
     "strengths": ["…"], "gaps": ["…"]},
    {"candidate_id": "9a7e…", "status": "pending", "screened": false, "reason": null, "reprocess_reason": null,
     "score": null, "eligible": null, "features": {}, "strengths": [], "gaps": []},
    {"candidate_id": "13bb…", "status": "blocked", "screened": false,
     "reason": "no consent record: record the candidate's consent (source and purpose) before screening",
     "reprocess_reason": null, "score": null, "eligible": null, "features": {}, "strengths": [], "gaps": []}
  ]
}
```

| `status` | Meaning | Dashboard hint |
|---|---|---|
| `scored` | Result available. `score`, `eligible`, `features`, `strengths`, `gaps` are filled. A feature value `null` means *not applicable*. | Show the comparison. |
| `pending` | In the run, no result yet. | "Screening in progress"; poll while `run_complete` is false. |
| `needs_reprocess` | In the run but could not be scored; see `reprocess_reason` (e.g. unreadable file, consent withdrawn). | Offer reprocessing / new run. |
| `blocked` | Excluded when the run was created; see `reason`. | Explain why (usually consent or a resume not ready). |
| `not_in_run` | Not an applicant of this job when the run was created. | "Not screened in this run". |

*Added:* `status`, `reason`, `reprocess_reason`, `run_status`, `run_complete`, `done`, `total`. Unscored rows now
return `null`/empty values instead of partial data.

Errors: `404` unknown job/candidate (or another company's), `404` `run_id` that belongs to a different job, `404`
"This job has no screening runs yet", `422` invalid body.

## Consent

Screening requires an active consent record. Revoking consent blocks new runs for the candidate and clears stored
extractions; existing results in older runs remain until the candidate is deleted.

### List consents

`GET /v1/candidates/{candidate_id}/consents` — `candidates:read` (*new*)

```json
{
  "candidate_id": "6c1d…",
  "has_active_consent": true,
  "consents": [
    {"consent_id": "…", "source": "email reply", "purpose": "recruitment screening",
     "granted_at": "2026-10-02T10:00:00Z", "revoked_at": null, "recorded_by": "5d2a…", "active": true},
    {"consent_id": "…", "source": "careers form", "purpose": "recruitment screening",
     "granted_at": "2026-09-01T10:00:00Z", "revoked_at": "2026-09-20T12:00:00Z", "recorded_by": "5d2a…",
     "active": false}
  ]
}
```

Newest grant first. `recorded_by` is the user who recorded the grant, or `null` if that account was removed.

### Record consent

`POST /v1/candidates/{candidate_id}/consents` — `consents:write`

```json
{"source": "careers form", "purpose": "recruitment screening", "granted_at": "2026-10-01T09:00:00+00:00"}
```

`source` 2–200 chars, `purpose` 2–300 chars, `granted_at` optional (default: now) and must include a timezone and
not be in the future. `201 {"consent_id": "…", "candidate_id": "…"}`. Errors: `422`, `404`.

### Revoke consent

`POST /v1/candidates/{candidate_id}/consents/revoke` — `consents:write`, no body.
`200 {"candidate_id": "…", "revoked": 1}` (`revoked` is the number of active grants withdrawn; `0` is not an error).
After revoking, `POST /v1/jobs/{job_id}/screening-runs` returns `409` with the candidate listed in
`details.blocked` if no other application can be screened.

## Export

`GET /v1/candidates/{candidate_id}/export` — `candidates:export` (admin)

Everything stored about the candidate, for a subject-access request:

```json
{
  "exported_at": "2026-10-05T10:00:00Z",
  "candidate": {"id": "…", "company_id": "…", "created_at": "…", "last_activity_at": "…"},
  "contact": {"name": "…", "email": "…", "phone": "…"},
  "consents": [], "registrations": [], "screening_inputs": [], "resumes": [],
  "resume_files": [{"resume_id": "…", "filename": "…", "mime": "application/pdf", "content_base64": "JVBERi0…"}],
  "applications": [], "screening_results": [], "decisions": [], "extractions": [], "audit_log": []
}
```

Keyed contact hashes are not exported. The export itself is audited (`candidate.exported`). The response can be
large (original files are inlined); offer it as a file download.

## Deletion (erasure)

`DELETE /v1/candidates/{candidate_id}` — `candidates:delete` (admin)

`200`:

```json
{"deleted": true, "candidate_id": "6c1d…", "resumes": 2, "blobs": 2, "applications": 1,
 "extraction_cache_entries": 2, "erasure_tombstones": 2}
```

*Added:* the counts. A second call returns `404`.

What it guarantees:

- One transaction, holding the same locks as ingestion, consent, decisions and screening: contact, resumes and their
  files, pending upload registrations, applications, decisions, consents, screening inputs and results, cached
  extractions and queued jobs for the candidate are all removed. Affected runs have their counts adjusted.
- A screening job that is running or waiting for a retry finishes as a no-op and recreates nothing.
- An upload of the same person registered **before** the erasure but processed after it is discarded (file
  included). This uses keyed contact hashes kept for `ERASURE_TOMBSTONE_DAYS` (default 30) — no plaintext.
- An upload registered **after** the erasure is treated as a new submission and creates a new candidate.
- The audit log records `candidate.deleted` with counts and the reason, without personal data.

## Retention settings

Candidates inactive for longer than the company's `retention_days` are deleted by a daily sweep at
`retention_sweep_hour_utc`. Each deletion re-checks inactivity under the candidate's lock, so a candidate who becomes
active after the sweep is skipped (`candidate.retention_skipped` in the audit log) and reconsidered by later sweeps.

### Read

`GET /v1/company` — `company:manage` (admin) (*new*)

```json
{
  "id": "2f6b…",
  "name": "Acme Hiring",
  "retention_days": 180,
  "retention_sweep_hour_utc": 2,
  "candidates_total": 412,
  "candidates_past_retention": 17
}
```

`candidates_past_retention` is the number of candidates the next sweep would delete under the current setting.

### Update

`PATCH /v1/company` — `company:manage` (admin)

```json
{"retention_days": 90}
```

`retention_days` is an integer from 1 to 3650 (`422` otherwise). Returns the same shape as `GET /v1/company`
(*added:* `retention_sweep_hour_utc`, `candidates_total`, `candidates_past_retention`). Shortening the period
deletes nothing immediately; the next sweep does. Show `candidates_past_retention` before confirming the change.

## Usage

`GET /v1/usage?from=2026-09-01&to=2026-10-01` — `usage:read` (admin)

- `from` (inclusive) and `to` (exclusive) are optional ISO 8601 dates or date-times. A value without a timezone is
  interpreted as UTC; a date is midnight UTC.
- `422` when a value is not a valid date/time, or when `from` is not earlier than `to`
  (`{"detail": "'from' must be earlier than 'to'"}`).

```json
{
  "from": "2026-09-01T00:00:00Z",
  "to": "2026-10-01T00:00:00Z",
  "totals": {"prompt_tokens": 175, "completion_tokens": 0, "ocr_pages": 2, "cost_usd": 0.175},
  "by_kind": {
    "llm": {"prompt_tokens": 125, "completion_tokens": 0, "ocr_pages": 0, "cost_usd": 0.125},
    "ocr": {"prompt_tokens": 50, "completion_tokens": 0, "ocr_pages": 2, "cost_usd": 0.05}
  },
  "runs": [{"run_id": "…", "job_id": "…", "prompt_tokens": 100, "completion_tokens": 0, "ocr_pages": 0,
            "cost_usd": 0.1}],
  "by_day": [
    {"date": "2026-09-01", "prompt_tokens": 150, "completion_tokens": 0, "ocr_pages": 2, "cost_usd": 0.15},
    {"date": "2026-09-03", "prompt_tokens": 25, "completion_tokens": 0, "ocr_pages": 0, "cost_usd": 0.025}
  ]
}
```

*Added:* `by_day` (UTC days that have usage, ascending; days without usage are omitted — fill gaps client-side).
`from`/`to` are echoed as normalised UTC values, or `null` when not given. Costs are rounded to 6 decimals.

## Talent pool

Semantic search across every candidate in the company, using the same embedding model as screening. Each
processed resume is indexed automatically by an `embed_resume` worker job, built from the redacted text (no
contact details). Results are **suggestions, not scores**: add a candidate to a job and run a screening for a score
and eligibility. Requires `SEMANTIC_BACKEND=embeddings`; otherwise the endpoints return `409`.

### Search

`POST /v1/talent-pool/search` — `candidates:read`

```json
{"query": "ML engineer with PyTorch and Kubernetes", "job_id": "47c2…", "limit": 20}
```

`query` (3–1000 chars), `job_id`, or both; `limit` 1–50 (default 20). With only `job_id`, the job's title and
description are searched. With `job_id`, `in_job` marks candidates who already applied to that job.

```json
{
  "model": "ollama:nomic-embed-text:v1.5",
  "query_source": "query",
  "job_id": "47c2…",
  "indexed_candidates": 412,
  "resumes_pending_index": 3,
  "results": [
    {"candidate_id": "6c1d…", "similarity": 0.7705, "name": "Lucas Moreau",
     "matched_resume": {"resume_id": "a3f0…", "version": 1, "created_at": "2026-10-06T02:26:10Z"},
     "applications": 1, "in_job": null}
  ]
}
```

`similarity` is cosine similarity of the best-matching resume per candidate, highest first. `name` is present
only for roles with `contacts:read`. `resumes_pending_index` counts processed resumes not yet indexed.
Errors: `404` unknown `job_id`, `409` embeddings not configured, `422` invalid body, `503` embedding model
unavailable (start the local AI server).

### Add a candidate to a job

`POST /v1/jobs/{job_id}/applications` — `resumes:write` (admin, recruiter)

```json
{"candidate_id": "6c1d…"}
```

Creates an application with the candidate's latest processed resume: `201` with
`{"application_id", "job_id", "candidate_id", "resume_id", "stage": "applied", "created": true}`, or `200` with
`"created": false` if they already applied. The next screening run includes them; consent is checked there, as for
uploads. Errors: `404` unknown job or candidate, `409` no processed resume.

### Re-index

`POST /v1/talent-pool/reindex` — `company:manage` (admin). `202 {"queued": 5}`: queues vectors for processed
resumes that have none for the current embedding model (for example after changing the model).

**Privacy.** Vectors are deleted with the candidate (erasure and retention) and are included in the candidate
export as `search_vectors`. Embedding usage appears in `/v1/usage` under `by_kind.embedding`.

## Eligible candidates from the talent pool

When a requirements version is confirmed, the worker checks every candidate with an active **talent-pool consent**
against that version, using the same extraction and eligibility (knockout) rules as screening. Candidates who
already applied to the job are skipped. Results are suggestions: nothing is added to the job until HR does it.

**Consent scope.** Consent records have `scope`: `"job"` (default; screening for the jobs applied to) or
`"talent_pool"` (also matching against future roles). Set it at upload (`consent.scope` in
`POST /v1/resumes/register`) or with `POST /v1/candidates/{id}/consents`. The consent list returns `scope`.
Revoking consent removes the candidate from every pool list immediately.

### Read matches

`GET /v1/jobs/{job_id}/pool-matches?version=2` — `candidates:read`. `version` defaults to the latest confirmed.

```json
{
  "job_id": "8bc5…",
  "requirements_version": 1,
  "confirmed": true,
  "pool_size": 2,
  "progress": {"total": 2, "checked": 2, "pending": 0, "failed": 0},
  "eligible": [
    {"match_id": "…", "candidate_id": "ab66…", "name": "Lucas Moreau", "resume_id": "…", "similarity": 0.7949,
     "status": "checked", "eligible": true, "reasons": [], "warnings": [], "failure_reason": null,
     "checked_at": "2026-10-06T03:58:10Z", "in_job": null}
  ],
  "ineligible": [{"candidate_id": "f0e0…", "eligible": false,
                  "reasons": ["has 1 of 6 required skills (needs 5); missing: SQL, …"], "…": "…"}],
  "failed": []
}
```

`eligible` is ordered by `similarity` (semantic closeness to the job description; `null` without embeddings).
`in_job` is the application stage once the candidate is added. `name` only with `contacts:read`. Poll while
`progress.pending > 0`. A version that is not confirmed returns `confirmed: false` and empty lists; an unknown
`version` is `404`.

### Add candidates

`POST /v1/jobs/{job_id}/applications/bulk` — `resumes:write`. Body `{"candidate_ids": ["…"]}` (1–500).
`200 {"job_id", "added": [...], "already_applied": [...], "failed": [{"candidate_id", "error"}]}`. Each candidate
is applied with their latest processed resume; then start a screening run for scores.

### Refresh

`POST /v1/jobs/{job_id}/pool-matches/refresh` — `screening:run`. `202 {"requirements_version": 1}`: checks the pool
again for the latest confirmed version, e.g. after new candidates joined. `409` if no version is confirmed.

## Related endpoints (unchanged)

| Endpoint | Permission | Note |
|---|---|---|
| `GET /v1/jobs/{job_id}/candidates?run_id=&stage=&eligible=&score_min=&score_max=&limit=&offset=` | `candidates:read` | Ranked list; `stage` filters by current application stage. |
| `GET /v1/candidates?email=&phone=&name=` | `contacts:read` | Search within the company; `422` without a criterion. |
| `GET /v1/resumes/{resume_id}` | `candidates:read` | `filename` is `null` without `contacts:read`. |
| `GET /v1/screening-runs/{run_id}` | `jobs:read` | Run status, `done`/`total`, `blocked`. |
| `GET/POST /v1/users` | `users:manage` | Create recruiters and hiring managers. |
