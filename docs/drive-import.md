# Google Drive import and resume search

Talent pool now has three sections: **Search resumes**, **Saved resumes**, and **Upload resumes**.
Saved resumes lets recruiters and admins browse every stored version without running a semantic search.
It shows processing status and active consent, with filters for consent, processing and failed files,
and direct links to each candidate's profile. Names and original files stay restricted to roles with contact access.
Local uploads and Drive imports can be added directly to the talent pool without a job.
The upload form separates computer files from Google Drive and explains when consent is missing.
No consent is inferred from an upload or recorded automatically.

Open a job's Resumes tab and use **Import from Google Drive** beneath the consent settings.
Paste a public Google Drive file or folder link, preview the files, select resumes and import.
The same consent settings apply as local uploads. Import does not imply candidate consent.
Files are registered individually through the existing duplicate detection and durable ingestion queue.
Per-file errors can be retried; successful files are skipped in the current preview.
Keep the browser tab open until registration finishes. Processing continues in the worker afterwards.

Only PDF, DOCX and TXT files directly inside the shared folder are listed. Folders with more than
500 resumes are supported: the preview shows 25 files per page, selection applies across every page,
and import continues automatically across batches of up to 500. Downloads run one at a time to bound
memory and network use. Pause waits for the current file to finish; restarting skips successful files.
Subfolders, Google Docs exports, private Google sign-in, resource-key-only links and quota-blocked
downloads are not supported. Change the sharing setting to “Anyone with the link” when appropriate.
No Google credentials or browser cookies are read. Downloads enforce the engine upload size limit
and only follow HTTPS redirects to explicitly allowed Google hosts.

The existing **Talent pool** page now presents **Resume search**. Describe the candidate or select
a job to search saved resume embeddings. Admins and recruiters can download the matching original
resume directly from results or from the candidate's resume history. Hiring managers retain access
to candidate reviews but cannot download originals containing contact details. Downloads require
authentication, enforce company isolation and are recorded in the audit log.

Endpoints: `POST /v1/drive/preview`, `POST /v1/drive/import`,
`GET /v1/resumes/{resume_id}/download`. See the API's `/docs` for request schemas.
