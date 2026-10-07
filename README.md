# Resume screening engine

Ranks resumes (PDF, DOCX, TXT) against one job description. Every candidate gets a 0–100 score, a per-feature
breakdown, a knockout status and evidence-backed strengths and gaps. An LLM (local Ollama or Azure OpenAI) only *extracts facts*,
each with a verbatim quote that is checked against the resume; **scores are always computed by deterministic
code**. The rules/TF-IDF profile runs without any model service. The free local AI profile uses downloaded
Qwen3 and Nomic models plus Tesseract OCR, with no paid AI API or API key.

## Free local AI

See [docs/free-local-ai.md](docs/free-local-ai.md) for the complete setup. On Windows, the helper installs a
checksum-verified portable Ollama runtime, the small models, and English OCR weights under `.cache/local-ai`:

```powershell
.\.venv\Scripts\python.exe scripts/local_ai.py setup
# For a fresh setup; preserve existing database/storage settings if you already have .env.
if (!(Test-Path .env)) { Copy-Item .env.local-ai.example .env }
.\.venv\Scripts\python.exe scripts/local_ai.py serve
```

Keep the model server running alongside the API and worker. The initial downloads need internet and several GB
of disk space. Inference uses your computer, so there are no AI API usage fees; hardware and electricity still
cost money. `shortlist-local:v1` is a project configuration of pretrained open weights, not a model trained from
scratch. The local profile processes one candidate at a time to fit computers with 8 GB RAM. HR still confirms
job requirements and reviews decisions; extraction quality needs evaluation on representative labeled resumes.

## Uploading large resume collections and training

The dashboard accepts **large resume selections, including more than 500 files**, with three simultaneous uploads, registration
progress, per-file retries and a paginated upload list. The durable worker queue handles extraction
and screening; keep the browser open until the files are registered.

**Start screening** asks whether to use saved resumes, uploads for the selected job, or both.
Saved candidates are attached automatically, and combined runs include each candidate once.
The picker shows counts from the current company workspace and keeps consent checks in place.
See [docs/screening-sources.md](docs/screening-sources.md) for the workflow and API.

See [docs/model-training.md](docs/model-training.md) for the synthetic dataset generator and optional
Qwen3 LoRA fine-tuning, held-out evaluation and Ollama import workflow. Preparation runs on this
laptop without a paid API key. The 120 synthetic starter examples verify the training pipeline;
they do not establish real-resume accuracy or replace the pretrained model automatically.

## Development setup

Python 3.11+ and Docker Compose v2.18+ are required for the standard local setup. Runtime dependencies are
pinned in `requirements.txt`; the `[dev]` extra installs pytest, Ruff, HTTPX and the package build tool.
`requirements-dev.txt` installs the project in editable mode, including `engine`, `api`, and `backend`, plus
the `[ltr]` extra so the LightGBM evaluation test runs along with the rest of the offline suite.

From the project root, in Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
docker compose up -d --wait
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m scripts.check_setup
.\.venv\Scripts\python.exe -m pytest -m "not live"
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

On Linux/macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
test -f .env || cp .env.example .env
docker compose up -d --wait
python -m alembic upgrade head
python -m scripts.check_setup
python -m pytest -m "not live"
python -m ruff check .
python -m ruff format --check .
```

Compose starts PostgreSQL 17 on `127.0.0.1:55432` and the Azurite blob emulator on `127.0.0.1:10000`, with
health checks and persistent named volumes. It creates a schema-owning `screening_system` role with BYPASSRLS
and a separate `screening_app` role subject to tenant row-level security. Migrations use `DATABASE_SYSTEM_URL`;
tenant requests use `DATABASE_URL`. The bootstrap PostgreSQL superuser is not used by the application.
The setup checker verifies both roles, the migration revision, enforced RLS, and a temporary SAS blob upload,
read and deletion. These checks use local services and do not call Azure OpenAI or OCR.

The database bootstrap script runs when the Docker volume is first initialized. Stop services with
`docker compose down`; their named volumes are retained. If you change a port, update its connection string
in `.env` as well. The values in `.env.example` are for local development; use separate random JWT and PII keys,
private cloud storage and the production database configuration when deploying. The Azurite account/key is
Microsoft's public emulator credential. See [connecting to Azurite](https://learn.microsoft.com/en-us/azure/storage/common/storage-connect-azurite).

### Without Docker

Use an installed PostgreSQL 17 and Node.js with npm. The PostgreSQL helper discovers binaries through `PG_BIN`,
then `PATH`, then the standard Windows installation directory. It uses the same SQL bootstrap as Compose.
Run these from the project root with the Python environment activated:

```bash
python scripts/local_pg.py start
npm exec --yes --package=azurite@3.37.0 -- azurite-blob --blobHost 127.0.0.1 --location .cache/azurite --skipApiVersionCheck
```

Azurite stays in the foreground; leave that terminal open and run the migrations and setup checker in a second
terminal. Copy `.env.example` to `.env` once before checking. Stop Azurite with Ctrl+C and PostgreSQL with
`python scripts/local_pg.py stop`. PostgreSQL stores this local cluster under `.cache/pgdata`.

Compose provides the dependencies only. Start the platform API and worker in separate terminals after the
migrations and setup checker pass. Use the activated Python environment:

```bash
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

```bash
python -m backend.worker
```

On PowerShell without activating the environment, replace `python` with `.\.venv\Scripts\python.exe`.
API documentation is at `http://127.0.0.1:8000/docs`. `/health` checks the running API; `/ready` checks both
database roles, the migrated workflow schema, and the storage container, returning 503 when a dependency fails.
Both processes refuse to start if their dependencies are unavailable, and close their database pools and
storage clients on shutdown. The platform uses JWT bearer authentication; `API_KEY` belongs to the separate
engine API below. Set the same stable `JWT_SECRET` and `PII_HASH_KEY` for every API and worker process.

The platform workflow in `/docs` is:

1. Register a company with `POST /v1/auth/register-company`, then authorize using its access token.
2. Create a job with `POST /v1/jobs`; analyze or manually edit requirements, then confirm their version.
3. Request `POST /v1/uploads/sas`, PUT the file bytes to `upload_url` with the returned headers, then call
   `POST /v1/resumes/register` with the job ID and the candidate's consent source and purpose.
4. Poll `GET /v1/resumes/{resume_id}` until ingestion is ready. Start `POST /v1/jobs/{job_id}/screening-runs`.
5. Poll `GET /v1/screening-runs/{run_id}` for progress, then read `GET /v1/jobs/{job_id}/candidates`.

Jobs, progress and results live in PostgreSQL and remain available after restarting either process.
Workers can run concurrently: claims use row locks and each attempt renews its lease. Failures use exponential
backoff and become dead jobs at `QUEUE_MAX_ATTEMPTS`. Exhausted ingestion jobs mark their resume failed;
exhausted screening jobs produce `needs_reprocess` results so their run can finish. If a worker crashes on its
last attempt, the next worker recovers this bookkeeping without repeating the handler. Terminal bookkeeping
is retried if the database is unavailable. Error messages stored by the runner contain exception types only.

Ctrl+C stops new claims and lets in-flight jobs finish for `WORKER_SHUTDOWN_TIMEOUT_S` (default 30 seconds).
Interrupted jobs are reclaimed after `QUEUE_LOCK_TIMEOUT_S`; handlers remain idempotent because execution is
at least once. A synchronous SDK/engine call already running in a thread may finish after cancellation, so
process exit can take longer than the drain timeout. `python -m backend.worker --once` processes at most one
job and exits, without scheduling retention. Normal workers enqueue a daily sweep per company at
`RETENTION_SWEEP_HOUR_UTC`, checking once a minute and catching up the most recently due date on startup.
Sweeps queue deletion for candidates beyond their company's configured retention period.
A deletion job re-checks inactivity under the candidate's lock and skips candidates active since the sweep.
Erasure leaves keyed contact hashes (no plaintext) for `ERASURE_TOMBSTONE_DAYS` (default 30) so uploads
registered before the erasure cannot recreate the person; sweeps purge expired hashes.

The standalone engine API remains available with `python -m uvicorn api.main:app --port 8000`; choose a
different port when running both APIs. Its background jobs remain in process memory.

### Durable screening and recovery

Run `python -m alembic upgrade head` before starting the updated API and workers. Migration
`0002_durable_workflow` adds the run snapshots, input records and upload associations, with enforced tenant RLS.
It preserves pending uploads from the earlier schema. Completed older runs remain readable; older runs without
saved inputs cannot be reconstructed reliably, so create a new run to screen them again.

Migration `0004_talent_pool` adds resume vectors for talent-pool search (no database extension needed).
Migration `0005_pool_matching` adds talent-pool consent and per-version eligibility of pool candidates.
Migration `0003_hr_management` records the stage before and after each decision (decision history is
append-only for the application role) and adds erasure tombstones. HR endpoints for Codex/dashboard
integration are described in [docs/hr-management-api.md](docs/hr-management-api.md).

Every new run saves the confirmed requirements, resolved weights, non-secret engine settings, model/deployment
names, prompt/ontology versions and a UTC reference date. Each application saves its resume ID, version,
processing generation and redacted text. Workers restore these inputs instead of reading a newer application
resume or their current scoring settings. API/worker credentials remain external and can be rotated. A source
and dependency fingerprint rejects incompatible worker versions explicitly; use the original version or
create a new run after changing the engine. Azure deployments must continue serving the intended model;
the project cannot prevent an operator changing a model behind a deployment name.

Successful candidate extraction is checkpointed before scoring. Queue retries and failed-run retries reuse
those verified facts, even when shared extraction caching is disabled. Extraction usage is recorded at that
checkpoint; a retry does not count it again. Changing requirements or using a newer resume requires a new run.
The run response includes `snapshot`, `reference_date` and `retry_of_run_id`; `GET /v1/screening-runs/{run_id}/inputs`
lists the frozen resume versions without returning their text.

Recovery endpoints are:

- `POST /v1/resumes/{resume_id}/reprocess`: retry ingestion for a `failed` or `needs_ocr` resume. Configure OCR
  before retrying a scanned PDF. Each request queues a fresh processing generation; old jobs cannot overwrite it.
- `POST /v1/screening-runs/{run_id}/retry`: once a run finishes, create a linked run for its `needs_reprocess`
  applications that currently have consent. It copies the original inputs/settings/date and preserves history.
- `POST /v1/candidates/{candidate_id}/consents/revoke`: withdraw every active grant and clear extraction caches
  and checkpoints. Both the worker's initial check and its final commit require active consent. Withdrawal
  during processing suppresses the score; an external call already in progress can finish. Earlier results
  remain historical records; the existing erasure endpoint removes the candidate's records and frozen text.

`POST /v1/resumes/register-bulk` returns one outcome per item, with 201 for a new resume, 200 for a duplicate,
and a status/error for rejected files. Registration and queue creation commit together for each item. Content
duplicates share one stored resume while retaining every job association, including uploads registered before
ingestion finishes. Replaying a duplicate registration also works after its redundant upload has been removed.
Registering an older version does not replace a newer application resume; a newer version clears the automatic
screened stage while preserving manual decisions. Replayed registrations do not reinstate withdrawn consent:
record a fresh grant with `POST /v1/candidates/{candidate_id}/consents`.

SAS upload URLs permit creation of one blob and cannot overwrite an existing blob. Send the file in the single
PUT described by the response; request a new URL to upload a new version. See the
[Azure Put Blob permissions](https://learn.microsoft.com/en-us/rest/api/storageservices/put-blob#service-sas).
Workers also verify the stored bytes against the registered content hash before ingestion.

### Backend integration checks

The normal offline suite includes API lifecycle/error and worker orchestration tests. PostgreSQL integration
tests are opt-in and require an **empty, dedicated test database** with the bootstrap roles and migrations.
They create and clean up their own companies; run them without other APIs/workers connected to that database.
Do not point these tests at a database containing candidate data. On PowerShell:

```powershell
$env:TEST_DATABASE_URL = 'postgresql+asyncpg://screening_app:app@127.0.0.1:55432/screening_test'
$env:TEST_DATABASE_SYSTEM_URL = 'postgresql+asyncpg://screening_system:system@127.0.0.1:55432/screening_test'
.\.venv\Scripts\python.exe -m pytest -m integration
```

Create `screening_test` with owner `screening_system` and grant it the same schema privileges as the local
database. Run `alembic upgrade head` with `DATABASE_SYSTEM_URL` pointing at that test database first.
Linux/macOS users can set the two `TEST_` variables with `export` and run `python -m pytest -m integration`.
Optionally set `TEST_BLOB_CONNECTION_STRING` to the local Azurite connection string for real SAS uploads;
otherwise these tests use memory storage. They verify persistence, RLS isolation, frozen inputs/settings,
mixed bulk uploads, concurrent deduplication, consent withdrawal, extraction checkpoints, reprocessing,
retention/deletion and refresh-token rotation without calling Azure OpenAI or cloud OCR. OCR recovery uses a
test adapter; live Azure validation is separate.

### Packaging checks

```bash
python -m build
python -m pip check
```

The wheel contains the engine, API, backend, and ontology/calibration JSON files. The source distribution also
contains local infrastructure, migrations, scripts, tests, and synthetic evaluation/sample data; it excludes
real candidate data and generated evaluation reports. Run migrations, tests and evaluation from the project
checkout or unpacked source distribution.

## HR dashboard (Phases 4–5)

The [recruiting workspace](docs/recruiting-workspace.md) adds a hiring pipeline, interview records and
calendar export, structured feedback, recruiter tasks, candidate notes, offer tracking and reports.
Run `alembic upgrade head` (through `0011_consolidation_references`) before starting the updated API. In the dashboard, use
**Recruiting workspace** or the recruiting actions on a candidate profile.

The [app improvement checklist](docs/app-checklist.md) tracks completed improvements and the remaining
recruiting and integration roadmap in implementation order. People workspace and employee/payroll modules are excluded.

**Hiring workflows** provides requisitions, assigned approvals, offer letter templates and new-hire handover.
**Recruiting workspace → Hiring setup** configures interview rounds and scorecards. Duplicate review,
follow-up workload and dated source/conversion reports are included. **Candidate portal** provides reviewed
job application pages and single-use interview booking links. Local links need a deployed app to be accessible
outside this computer; connected calendars, email, job boards and signature/check providers need account setup.

The React/TypeScript dashboard is in [`frontend/`](frontend/README.md). After starting the platform API,
worker, PostgreSQL and Azurite above, run `npm ci` and `npm run dev` from that directory, then open
`http://127.0.0.1:5173`. It provides sign-in, jobs, requirement review/confirmation, bulk uploads,
processing progress, ranked results, candidate evidence, comparison, decisions/history, consent,
export and erasure. Admin screens provide team accounts, retention settings and daily usage/cost.
The frontend README includes setup, role permissions, browser tests and the Phase 5 API contracts.

## Quick start (offline)

Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt  # runtime deps + pytest, ruff

python -m engine.pipeline --jd data/sample/jd.txt --resumes data/sample/resumes/ \
    --confirmed-by "Your Name" --today 2026-10-05
```

Requirements extracted from a JD must be reviewed by a person before screening. Without `--confirmed-by`, the
CLI prints the extracted requirements and exits with code 2; alternatively save them, edit them, and pass them with
`--requirements reviewed.json`. Output (abridged):

```
RANKED (2 eligible)
  #  score  file                          top contributions / gaps / N/A
  1   84.1  01_strong_match.pdf           required_skill_coverage=28.4, relevant_experience=20.0, semantic_relevance=14.7
                                       + Relevant experience: 112 relevant months vs 60 required in machine learning ...  ["Senior Machine Learning Engineer | ..."]
  2   61.2  02_overlapping_jobs.docx      required_skill_coverage=23.7, relevant_experience=18.7, semantic_relevance=8.6
                                       - Missing required skills: Kubernetes

INELIGIBLE (5, not ranked)
      78.8  07_location_notice.txt        notice period 90 days exceeds maximum 60 days
       5.0  03_hidden_injection.pdf       has 0 of 6 required skills (needs 5); ...

NEEDS REPROCESSING (1, not scored)
            08_scanned_needs_ocr.pdf      needs_ocr: only 36 characters of text; OCR required
```

Add `--json result.json` for the full result. The sample data (`data/sample/`) is synthetic; regenerate it with
`python data/sample/generate_samples.py`.

## How it works

```mermaid
flowchart LR
    JD[JD text] --> AN["extract_job_requirements<br/>(LLM or rules)"] --> HR{{HR review<br/>requirements_confirmed}}
    R[Resumes PDF/DOCX/TXT] --> P["parsing<br/>hidden-text stripping,<br/>2-column reading order, OCR flag"]
    P --> C[(extraction cache)]
    C --> X["extract_candidate<br/>(LLM or rules)"] --> V["evidence verification<br/>quotes must be in the resume"]
    V --> N["normalize<br/>skills, dates, overlaps, seniority"]
    HR --> K[knockouts]
    N --> K --> F["features (6)"] --> S["scorer<br/>weighted sum, N/A renormalized"]
    S --> E["explain<br/>features + verified quotes only"]
    E --> OUT["ranked / ineligible / needs_reprocess"]
```

| Stage | Module | Key rules |
|---|---|---|
| Parsing | `engine/parsing.py` | Strips hidden text (white or near-white, < 4 pt, invisible, off-page, hidden Word runs, invisible Unicode) to block prompt injection; ZWJ/ZWNJ kept inside Indic scripts; two-column PDFs read left column then right; PDFs with < 200 characters use Azure Document Intelligence when configured, otherwise remain flagged `needs_ocr`. |
| Extraction | `engine/extraction.py`, `engine/prompts.py` | Temperature 0, resume fenced as untrusted data, structured outputs. Every item carries an `evidence` quote; items whose quote is not in the resume, or does not support the item, are dropped and counted. Job dates must be a date range near the title (layout detected per resume, each range claimable once). |
| Mode | `EXTRACTION_MODE` | `llm` or `rules`, chosen once per run, never mixed in a batch. An LLM failure after 3 retries puts the resume in `needs_reprocess`. |
| Normalization | `engine/normalize.py`, `engine/skills_ontology.json` | ~309 canonical skills (tech plus 24 non-technical professions) with aliases and inflections. Ambiguous aliases (`go`, `r`, `cv`, `excel`, `word`, `sales`, and everyday words that also appear in employer names such as `logistics` or `nursing`) only count as items in a skills list. Overlapping jobs are counted once; year-only or start-only dates are inferred with `date_confidence="low"`. Career gaps are never used. |
| Knockouts | `engine/knockout.py` | Required skills (default `ceil(0.8 × n)`, or `"all"` per job), minimum experience, location, notice period. Ineligible candidates are kept, with reasons; missing data is a warning, not a knockout. |
| Features | `engine/features.py`, `engine/semantic.py` | `required_skill_coverage` (recency-weighted), `relevant_experience`, `project_relevance`, `seniority_fit`, `preferred_skill_coverage`, `semantic_relevance`. A feature can be N/A; weights are renormalized over applicable features. |
| Explanations | `engine/explain.py` | At most 4 strengths and 4 gaps. The LLM sees only feature outputs and verified quotes; strengths whose quote is not in the evidence list are dropped. Missing required skills and knockouts are always listed as gaps; career gaps never are. Offline: templates. |

Every result records the prompt versions, model, ontology version, weights, extraction-cache hits and who confirmed
the requirements.

## Configuration

All configuration is read from environment variables (or a `.env` file).

| Variable | Default | Purpose |
|---|---|---|
| `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY` | unset | Enable the LLM and embeddings paths. Unset means offline. |
| `AZURE_OPENAI_API_VERSION` | `2024-10-21` | API version (structured outputs). |
| `AZURE_OPENAI_CHAT_DEPLOYMENT` / `_EMBEDDING_DEPLOYMENT` | `gpt-4o` / `text-embedding-3-small` | Deployment names. |
| `AZURE_DOCINTEL_ENDPOINT`, `AZURE_DOCINTEL_KEY` | unset | Enable Azure Document Intelligence OCR for scanned PDFs. |
| `OCR_TIMEOUT_S`, `OCR_PRICE_PER_PAGE` | `120`, `0.0015` | OCR request timeout and per-page USD cost used for logging. |
| `EXTRACTION_MODE` | from credentials | `llm` or `rules`. `llm` without credentials fails at startup. |
| `EXPLANATION_MODE` | `llm` | `llm` or `template`. `template` writes strengths/gaps from feature results without a model call; scores are unchanged. |
| `SEMANTIC_BACKEND` | from credentials | `embeddings` or `tfidf`. |
| `SEMANTIC_RERANKER` | `none` | `bge-reranker-v2-m3` enables a cross-encoder for `semantic_relevance` (needs `pip install .[rerank]`). |
| `LLM_MAX_RETRIES`, `LLM_BACKOFF_BASE_S`, `LLM_TIMEOUT_S` | `3`, `1.0`, `60` | Retries with exponential backoff. |
| `LLM_PRICE_INPUT_PER_1K`, `LLM_PRICE_OUTPUT_PER_1K`, `LLM_PRICE_EMBEDDING_PER_1K` | `0.0025`, `0.01`, `0.00002` | USD prices used for cost logging. |
| `SCORE_WEIGHTS` | see `engine/config.py` | JSON object of feature → weight; a job's `score_weights` overrides it. |
| `KNOCKOUT_REQUIRED_SKILL_RATIO` | `0.8` | Default share of required skills; a job's `required_skills_min_match` (an int or `"all"`) overrides it. |
| `KNOCKOUT_EXPERIENCE_TOLERANCE_MONTHS` | `0` | Grace period on minimum experience. |
| `SKILL_RECENCY_*`, `SKILL_UNKNOWN_RECENCY_WEIGHT` | `36`/`120` months, `0.6`, `0.5` | Recency weighting; validated so dated use always outweighs listed-only. |
| `EXTRACTION_CACHE_ENABLED`, `EXTRACTION_CACHE_DIR` | `true`, `.cache/extractions` | Job-independent extraction cache. |
| `SCREEN_MAX_WORKERS` | `4` | Resumes processed concurrently per screening run. |
| `MAX_FILE_BYTES`, `MAX_FILES_PER_REQUEST`, `MAX_JD_CHARS` | 5 MiB, `100`, `50000` | Input limits. |
| `API_KEY` | unset | When set, API calls (except `/health`) need `X-API-Key`. |
| `MAX_STORED_JOBS` | `500` | Finished screening jobs kept in memory. |
| `FAIRNESS_SCOPE` | `ci` | Evaluation fairness check: `ci` (3 cases × 6 variants) or `full` (all cases × 28 variants). |
| `LOG_LEVEL`, `LOG_JSON` | `INFO`, `true` | Structured JSON logs (latency, tokens, cost per resume; no resume text). |

## HTTP API

```bash
uvicorn api.main:app --port 8000          # single worker: job state is in process memory
```

| Method | Path | Body | Response |
|---|---|---|---|
| `GET` | `/health` | — | `{"status": "ok", "extraction_mode", "semantic_backend", "auth_required"}` |
| `POST` | `/jobs/analyze` | `{"jd_text": "..."}` | `JobAnalysisResult`: requirements with `requirements_confirmed: null`, for HR review |
| `POST` | `/screen` | multipart: `requirements` (JSON with `requirements_confirmed`), `files` (1–100 resumes) | `202 {"job_id", "status": "queued", "status_url"}` |
| `GET` | `/screen/{job_id}` | — | `{"status": "queued\|running\|completed\|failed", "progress": {"done", "total"}, "result", "error"}` |

```bash
curl -s localhost:8000/jobs/analyze -H 'content-type: application/json' \
     -d "{\"jd_text\": $(jq -Rs . < data/sample/jd.txt)}" | jq .requirements > req.json
# Review req.json, then mark it confirmed:
jq '.requirements_confirmed = {"by": "Jane HR", "at": "2026-10-05T10:00:00Z"}' req.json > confirmed.json

curl -s localhost:8000/screen -F "requirements=<confirmed.json" \
     -F files=@data/sample/resumes/01_strong_match.pdf -F files=@data/sample/resumes/05_intern.txt
curl -s localhost:8000/screen/<job_id> | jq '.status, .result.ranked[].score'
```

Errors: `401` bad or missing API key; `413` file, request or JD too large; `415` unsupported or mismatched file type;
`422` invalid or unconfirmed requirements; `400` empty file; `404` unknown job; `502` LLM failure in
`/jobs/analyze`. Resumes that cannot be processed are returned in `result.needs_reprocess` rather than failing the job.

## Docker

```bash
docker build -t resume-screener .
docker run --rm -p 8000:8000 -e API_KEY=change-me resume-screener          # offline
docker run --rm -p 8000:8000 --env-file .env resume-screener               # with Azure OpenAI
```

The image is `python:3.11-slim` and runs as a non-root user (`uid 10001`); only `/app/.cache` is writable. Mount a
volume at `/app/.cache` to keep the extraction cache across restarts.

## Tests and evaluation

```bash
pytest                       # offline suite (live Azure tests are skipped without credentials)
pytest -m live -s            # live Azure OpenAI tests (credentials required)
python scripts/smoke_azure.py   # one JD + one resume end to end, printing tokens, latency and cost
ruff check . && ruff format --check .

python -m eval.run_eval      # golden-set report; exits 1 on regression (CI gate)
python -m eval.run_eval --update-baseline   # accept current metrics
python -m eval.run_eval --calibrate         # refit similarity "low" thresholds for the current backend
python -m eval.run_eval --ltr               # also compare LambdaMART (pip install .[ltr])
```

The golden set (`eval/golden/`, generated by `eval/golden/generate_golden.py`) is synthetic: 3 jobs, 16 resumes
and 48 labels. It covers every date layout (above, below, same-line, two-column, sidebar, year-only, start-only,
mixed), resumes without projects, and one with no jobs. `run_eval` reports:

- NDCG@10, precision@5 and recall of label ≥ 2 in the top 20%;
- skill-extraction F1 and date accuracy;
- evidence drop rates per field and prompt version;
- counterfactual fairness, swapping names across Indian regions, communities and genders, plus pronouns. The score delta must be under 1 point and the explanation must be unchanged;
- similarity thresholds, in-sample and leave-one-job-out.

It writes `eval/reports/error_analysis.md`, with a root cause for every relevant candidate outside the top 20% and
every top-5 false positive. Baselines are kept per extraction mode in `eval/baseline.json`.

### Labelled resume sets (e.g. the Kaggle resume dataset)

A folder or `.zip` with one sub-folder per profession (such as the public
[Kaggle resume dataset](https://www.kaggle.com/datasets/snehaanbhawal/resume-dataset)) can be imported and measured:

```bash
# Bulk-import a stratified sample into a separate test workspace (talent-pool consent labelled as test data)
python scripts/import_resumes.py resume-dataset.zip --create-workspace "Kaggle test data" --per-group 20 \
    --consent-source "Kaggle example resumes (test data)" --talent-pool --map kaggle-import.csv \
    --credentials-out kaggle-workspace.json
# Talent-pool search precision per profession, once indexing finishes
python -m eval.talent_pool_eval --map kaggle-import.csv --credentials kaggle-workspace.json --wait
# Skill dictionary on a held-out half: coverage and per-profession eligibility, compared with an older version
python -m eval.skill_dictionary_eval resume-dataset.zip --baseline-ontology old_skills_ontology.json
```

Professions are a coarse label: these scripts compare search and dictionary versions; they do not measure hiring
accuracy. Use a separate workspace for test data, and record consent only as actually obtained.

### Evaluating on real data

Put a real labelled set in `eval/real/`. Everything there except its README is git-ignored. Use the same layout as
the golden set:

```
eval/real/
  golden.json        {"reference_date": "2026-10-05", "jobs": ["backend_2026q4", ...]}
  jobs/<job_id>.txt  job descriptions
  resumes/<file>     resumes as received (PDF, DOCX or TXT)
  labels.jsonl       one line per (job, resume): {"job_id": "backend_2026q4", "candidate_file": "c0412.pdf", "label": 2}
  annotations.jsonl  optional: {"candidate_file", "layout", "has_projects", "skills": [...],
                                "jobs": [{"title", "start": "YYYY-MM", "end": "YYYY-MM" | "present", "approximate"}]}
```

Labels are 0–3 (0 not relevant … 3 strong match), and every resume needs a label for every job. Annotations are
optional; skill F1 and date accuracy are computed only on annotated resumes. Run
`python -m eval.run_eval --real`. The error analysis goes to `eval/real/reports/`, and the baseline key is
`real:<mode>`.

PII handling:

- Rename files to opaque IDs (`c0412.pdf`), never candidate names; filenames appear in reports.
- Keep the set on encrypted storage with access limited to the evaluation team; it must never be committed, copied
  into issues or chats, or pasted into prompts outside the engine.
- With `EXTRACTION_MODE=llm`, resume text is sent to your Azure OpenAI deployment. Confirm that the deployment's
  data-processing terms (region, retention, abuse monitoring) cover candidate data.
- The extraction cache (`.cache/extractions`) stores verified extracts, which can include quotes; clear it after
  evaluating real data.
- Delete the set when the evaluation purpose ends, per your retention policy.

**Consent:** use only resumes that candidates submitted under terms permitting their use to evaluate and improve the
screening system, or obtain explicit consent, and follow applicable law (for example the DPDP Act in India, or GDPR).
Record the legal basis alongside the set. Labels should come from trained reviewers following a written rubric.

## Production readiness

Done:

- Hidden-text and prompt-injection defences, evidence verification, PII-free extraction schema.
- Human confirmation of requirements, knockouts that keep candidates visible, and explanations limited to verified quotes.
- Retry and backoff, a cost and latency log for every resume, an extraction cache, bounded concurrency.
- A golden-set CI gate with fairness counterfactuals.

Still needed:

1. **Run against real Azure OpenAI.** The live tests, `scripts/smoke_azure.py`, and `eval.run_eval` in `llm` mode
   have only run offline so far; set the `llm` baseline and run `--calibrate` for the `embeddings` backend.
2. **OCR validation.** The Azure Document Intelligence adapter is implemented. Test it against real scanned PDFs
   with `AZURE_DOCINTEL_ENDPOINT` / `AZURE_DOCINTEL_KEY`; unconfigured scans go to `needs_reprocess`.
3. **Real labelled data.** The golden set is synthetic and was labelled by the engine's author. Collect a consented,
   rubric-labelled real set (see above), then refit weights (`fit_weights`) and thresholds on it.
4. **Durable job state.** Replace the in-memory `JobStore` and the file extraction cache with Postgres or Redis
   before running more than one API worker or replica, and add TTL and retention for results.
5. **Authentication and authorization.** `API_KEY` is a single shared key; put the API behind your identity
   provider, with per-tenant authorization and audit logging of who screened which job.
6. **Abuse limits.** Add request rate limiting and an upload size limit at the reverse proxy (the app also limits
   per-file size).
7. **Bias monitoring.** Track selection rates by job, run `FAIRNESS_SCOPE=full` before every model or prompt change,
   and review adverse-impact ratios on real outcomes.
8. **Container build.** The Dockerfile has not been built in this environment.
