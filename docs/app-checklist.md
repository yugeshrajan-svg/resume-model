# App improvement checklist

Updated October 7, 2026. Work through each item in order and check it only after implementation and validation.

## Current pass: everyday recruiting

- [x] 1. Find candidates quickly: search the hiring pipeline and record forms by candidate name, resume filename or candidate ID; keep the form picker independent of stage filters and pagination. Database search/privacy and browser workflows verified.
- [x] 2. Find the work that matters: activity search, status and owner filters, overdue tasks, and clear candidate/job labels on records. Database filters/privacy and browser workflows verified.
- [x] 3. Correct and reschedule records: edit tasks, interviews and offers with validation, interview conflict checks, audited updates and protection against stale edits. Feedback and notes remain historical records. Database edit/conflict/access checks, browser rescheduling, build and lint verified.
- [x] 4. Review the candidate's recruiting history: a combined, paginated activity view on the profile, respecting existing role restrictions. Role-based profile/history filtering and the wider browser suite verified.

## Next pass: complete the local hiring workflow

- [x] 5. Configurable interview rounds and job-specific feedback scorecards. Immutable versions and stored ratings verified.
- [x] 6. Requisition and offer approvals with assigned reviewers and recorded decisions. Self-approval and changed offer terms blocked.
- [x] 7. Offer letter templates and downloadable documents with a review step. Printable HTML letters, escaped text, current offer approval checks; no sending or signing.
- [x] 8. Candidate duplicate review and safe resume/profile consolidation. Admin confirmation; overlapping applications/active processing blocked; files, history, aliases and erasure verified.
- [x] 9. In-app follow-up reminders and workload views. Assigned work, overdue and due-soon counts; no outside notifications.
- [x] 10. Reporting date ranges, source tracking and conversion trends. UTC application cohorts, sources and monthly stage/hire conversion. Historical missing sources remain "Not recorded".
- [x] 11. New-hire handover and onboarding checklists after a human records a hire. Checklist actions and recorded history.

## Integrations: select and configure providers before connecting

- [ ] 12. Google / Microsoft calendar connection, availability and invitations.
- [ ] 13. Recruiting email connection, templates and communication history.
- [ ] 14. Job-board publishing and external hosting. Public job pages/application forms are implemented and tested locally; candidate-provided consent is required. Outside applicants need a deployed app, and job-board distribution needs a provider.
- [x] 15. Candidate self-scheduling using single-use links and offered slots. Local interviewer/candidate conflicts, expiry and replay checks verified; external calendar connection remains item 12.
- [ ] 16. E-signature and assessment/background-check providers.

## Removed from scope at the user's request

People workspace, employee directory/documents, leave, attendance, employee self-service,
performance, learning, payroll and expenses are not part of this app. Continue the recruiting / ATS checklist above.

Existing features include local/Drive resume uploads, saved resume library, semantic resume search,
screening, consent records, company-scoped access, recruiting stages, interview/feedback/task/offer records,
calendar export, reports, retention, candidate data export and erasure. Consent must come from an actual
recorded source and purpose; existing resumes do not receive invented consent.

External connections need a chosen provider and account setup. Items 12, 13, 14 and 16 remain open;
the app does not show a provider as connected when it has not been configured. Calendar and email provider
selection has been requested. Employee/payroll providers are no longer needed.

Validation: frontend build/lint, 34 unit checks, 15 browser workflows including accessibility/mobile checks,
backend recruiting/approval/privacy integration checks and an empty-database migration through head.
