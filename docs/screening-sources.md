# Choose resumes for a screening

Open any job, confirm its requirements, and click **Start screening**. The dashboard asks which resumes to use:

| Choice | Inputs |
| --- | --- |
| Saved resumes | The latest ready resume for each candidate in the current company with active talent-pool consent. |
| Uploaded resumes | Candidates whose resumes were uploaded to this job. |
| Both sources | The union of those sources, with each candidate included once. A job upload takes precedence if the candidate appears in both. |

The dialog shows counts before starting. **Upload new resumes** opens the job's upload tab; **Continue to screening** keeps the selected upload/both choice. Upload batches still accept 1–500 files. Finish uploading and processing before starting an upload or combined run.

Saved candidates are automatically added to the job when the run starts. The preview does not create applications. Selecting saved resumes includes candidates who fail a job's knockout rules: the completed screening explains those failures rather than hiding them before scoring.

Uploaded resumes use the latest version uploaded to the selected job. A saved-resume run can use a newer version without removing the original upload from future upload-only runs.

Only the current company's resumes are used. For example, resumes imported into a separate Kaggle test workspace are available in that workspace, not in another company's job. No resume data is copied between companies.

## API

`GET /v1/jobs/{job_id}/screening-sources` requires `candidates:read` and returns:

```json
{
  "database": {"total": 2, "screenable": 2, "blocked": 0},
  "uploads": {"total": 2, "screenable": 2, "blocked": 0},
  "both": {"total": 3, "screenable": 3, "blocked": 0},
  "pending_uploads": 0,
  "database_excluded": 1
}
```

Counts are candidate counts, not file counts. `database_excluded` counts candidates whose saved resumes are not ready or lack active talent-pool consent. Uploaded candidates without consent or a ready resume appear in the selected run's existing `blocked` list. Pending uploads prevent starting upload/combined runs so unfinished files are not silently omitted.

`POST /v1/jobs/{job_id}/screening-runs` requires `screening:run`. Pass exactly one source:

```json
{"source": "both"}
```

Allowed values are `database`, `uploads`, and `both`. A request without a body retains the existing API behavior of screening all applications already attached to the job. Invalid source values and extra request fields return 422. A selection with no screenable candidates returns 409. Cross-company job IDs return 404.

The run snapshot records `resume_source`, `source_selected_at`, and `source_counts`. Candidate IDs, resume versions, extraction settings, requirements, weights and reference dates remain frozen in screening inputs and the existing snapshot. Retries reuse the same inputs and source choice. Saved inputs continue to require talent-pool consent during worker execution and retries, even if a later upload gives that person a separate job consent. Manual review decisions are preserved when a saved candidate's latest resume is attached.

No migration is needed beyond the existing schema. Restart the API and workers together after updating the code; the worker fingerprint must match newly created runs.
