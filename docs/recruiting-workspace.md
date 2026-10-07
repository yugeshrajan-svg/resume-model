# Recruiting workspace

The application now includes a recruiting / ATS workspace at `/recruiting`. All records are stored
in PostgreSQL and scoped to the company, with database row-level security and audited updates.
Run `alembic upgrade head` through `0011_consolidation_references` before starting this version.

## Workflow

1. Create a job, confirm requirements, and add candidates through job uploads or the talent pool.
2. Open **Recruiting workspace → Hiring pipeline** and select a job. Applications are available before
   screening. Filter stages and open a candidate profile. Changing stage requires a reason and writes
   immutable decision history; stale concurrent changes return a conflict rather than overwrite.
   Search by candidate name, resume filename or candidate ID. Hiring managers can search by ID.
   The candidate picker in record forms has its own search and pagination, independent of pipeline stages.
3. From the profile, schedule an interview, add a follow-up task or note, record interview feedback,
   or record an offer. Job-linked records can also be added directly in the workspace.
4. Tasks have an owner and optional deadline. Interviews have start/end times, interviewer ownership,
   location or meeting text, and downloadable `.ics` events. Overlapping open interviews for the same
   interviewer or candidate are rejected. Times are stored as timezone-aware UTC values and displayed
   in the viewer's device timezone.
5. Feedback records contain role skills, communication and experience ratings from 1–5, a recommendation,
   and supporting notes. Feedback and notes are immutable records. Add another record to supplement them.
6. Offers record a compensation amount, currency code, pay period, intended start date, notes and an
   open/accepted/declined/cancelled status. These are internal records; they do not sign contracts.
7. Reports show application counts per job/stage, open and overdue tasks, open interviews/offers,
   hires recorded in the last 30 days, and average days from application to the first recorded hire.
   CSV export includes job/stage counts and protects spreadsheet cells from formula interpretation.
8. Search recruiting records and filter by job, status, owner or overdue tasks. Records show candidate
   and job context. **Edit record** or **Edit / reschedule** changes tasks, interviews or offers without
   changing their candidate/job links. Editing and reopening interviews checks scheduling conflicts.
   The UI sends the last loaded update timestamp to prevent overwriting another teammate's changes.
   Download a new calendar event after changing interview details; earlier downloaded files do not update automatically.
9. Candidate profiles show a combined **Recruiting history** with type filters and pagination, across
   all the candidate's jobs. Application decision history continues to show stage changes separately.

Pipeline stages are Applied, Screened, Review, Shortlisted, Interview, Offer, Hired, Rejected, Withdrawn.
Screening continues to preserve manual stages. Stage changes never send communications or make a
candidate decision automatically. Interviews, tasks, offers and stage changes update candidate activity
for retention. Candidate export includes recruiting records; erasure cascades to these records.

## Permissions

Admins and recruiters manage stages, tasks, interviews, notes and offers. Hiring managers can read
recruiting records and submit structured interview feedback, while original resume files, candidate
contact names and offer compensation remain restricted. Team owner labels follow contact visibility.
Every candidate, job, application and assigned user is checked against the active company before writes.

## Market comparison and next integrations

Reviewed October 7, 2026 against vendor-published categories:

- [Workable platform features](https://www.workable.com/features/): applicant tracking, interview
  coordination, assessments, offers, reporting and HR integrations.
- [Greenhouse hiring platform](https://www.greenhouse.com/platform): structured hiring and recruiting workflows.
- [Zoho People features](https://www.zoho.com/en-us/people/features.html): employee management, onboarding,
  leave and attendance belong to the wider HR suite, outside the recruiting-first scope selected here.

The app stays focused on recruiting. People workspace, employee self-service, payroll, attendance, leave,
performance and learning are excluded at the user's request. Connected calendar invitations/availability,
recruiting email, job-board distribution, e-signature and external assessment/background checks still need
chosen providers and account setup. No communications are sent by the local workflows below.

## Additional local hiring workflows

- **Recruiting workspace → Hiring setup:** save versioned interview rounds with 1–12 criteria per round.
  Job-linked interviews and feedback use the selected round. Feedback retains the original plan and ratings
  even when a recruiter changes the plan later. Stale versions are rejected.
- **Hiring workflows:** submit requisitions or open offers to another assigned reviewer. Only that reviewer
  can approve/reject, with a reason and recorded history. Approved requisitions create a job for the normal
  requirements review. Offer approvals snapshot the offer terms; changing them requires fresh approval.
- **Offer letter templates:** create text using the supported placeholders. On an offer, review its terms
  and selected template, then download a printable HTML letter. This can be printed to PDF. Documents are
  escaped, require current approved terms when an approval exists, and are neither sent nor signed.
- **Duplicate review:** compare matching-name hints and explicitly confirm the same person before an admin
  consolidates profiles. Overlapping job applications, active processing and unfinished resumes block the
  operation. Files, application IDs, consent, screening evidence and append-only audit history are preserved.
  Original resume version numbers are retained; alternate identity hashes continue deduplication and erasure.
- **Overview → Follow-up workload:** shows assigned open tasks/interviews, overdue counts, work due within
  seven days and team workload. Reminders update inside the app; they are not external notifications.
- **Reports:** UTC application date ranges, newly recorded upload/Drive/public application sources, historical
  stages reached and monthly application cohort conversion (latest 24 nonempty months). Existing sources are
  explicitly "Not recorded". Live workload and last-30-day hiring totals are independent of cohort date filters.
- **New-hire handover:** start a checklist only after a human records a hire. Complete individual checklist
  items and retain action/edit history. This is a recruiting handover, without an employee workspace.
- **Candidate portal:** review and create a public job page with a resume application form and the applicant's
  own consent. Create single-use interview booking links with offered slots within 14 days, up to four hours
  each. Booking rechecks local interview conflicts, active team accounts and the interview plan. Close unused
  links from the portal. Links on localhost are previews; outside candidates require a deployed application.

## APIs

- `GET /v1/recruiting/team`
- `GET /v1/jobs/{job_id}/pipeline` (stage filter, offset; 50 applications per page)
- `PATCH /v1/applications/{application_id}/stage` (stage, reason, expected_stage)
- `GET /v1/recruiting/activities` (kind, job, candidate and offset filters; 50 per page)
- `POST /v1/recruiting/activities`
- `POST /v1/recruiting/feedback`
- `PATCH /v1/recruiting/activities/{activity_id}` (record status)
- `PATCH /v1/recruiting/activities/{activity_id}/details` (editable fields and required expected_updated_at)
- `GET /v1/recruiting/activities/{activity_id}/calendar`
- `GET /v1/recruiting/reports`
- `GET /v1/recruiting/workload`
- `GET|PUT /v1/jobs/{job_id}/hiring-plan`
- `GET /v1/hr/catalog`, `GET /v1/hr/team`, `GET|POST /v1/hr/records`
- `PUT /v1/hr/records/{id}`, `POST /v1/hr/records/{id}/action`
- `POST /v1/recruiting/offers/{id}/letter`
- `GET /v1/talent-pool/duplicates`, `POST /v1/candidates/merge/preview`, `POST /v1/candidates/merge`
- `POST /v1/portal/jobs`, `POST /v1/portal/booking-links`, `GET /v1/portal/links`, `POST /v1/portal/links/{id}/close`
- Public: `GET /v1/public/jobs/{id}`, `POST /v1/public/jobs/{id}/apply`, `GET|POST /v1/public/book/{id}`

See `/docs` for validation schemas. Feedback does not affect deterministic resume scores.

See [App improvement checklist](app-checklist.md) for completed work and the remaining roadmap.
