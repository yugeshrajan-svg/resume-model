"""HTTP API.

  GET  /health               liveness + run-level configuration
  POST /jobs/analyze         JD text -> JobRequirements (unconfirmed) for HR review
  POST /screen               confirmed requirements + resume files -> 202 {job_id}; screening runs in the background
  GET  /screen/{job_id}      progress, then the ranked result

Screening jobs live in process memory: run a single worker process (or replace ``JobStore`` with a
shared store such as Postgres/Redis before scaling out). Extraction results are cached on disk and
shared across jobs.
"""

from __future__ import annotations

import hmac
import logging
import threading
import uuid
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field, ValidationError

from engine import __version__
from engine.cache import ExtractionCache, get_extraction_cache
from engine.config import ConfigError, Settings, get_settings
from engine.extraction import ExtractionFailed, extract_job_requirements
from engine.llm import get_llm
from engine.logging_setup import configure_logging, log_event
from engine.parsing import FileTooLarge, ParseError, UnsupportedFileType, detect_file_type
from engine.pipeline import screen
from engine.schemas import JobAnalysisResult, JobRequirements, ScreeningResult

logger = logging.getLogger("engine.api")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class AnalyzeRequest(BaseModel):
    jd_text: str = Field(min_length=20, description="Job description text")


class ScreenAccepted(BaseModel):
    job_id: str
    status: Literal["queued"]
    status_url: str
    documents: int


class Progress(BaseModel):
    done: int
    total: int


class ScreenJob(BaseModel):
    job_id: str
    status: Literal["queued", "running", "completed", "failed"]
    progress: Progress
    created_at: datetime
    finished_at: datetime | None = None
    error: str | None = None
    result: ScreeningResult | None = None


# ---------------------------------------------------------------------------
# Job store (in memory, thread-safe)
# ---------------------------------------------------------------------------


class JobStore:
    """Screening jobs by id. Finished jobs beyond ``capacity`` are evicted oldest first."""

    def __init__(self, capacity: int) -> None:
        self._jobs: OrderedDict[str, ScreenJob] = OrderedDict()
        self._lock = threading.Lock()
        self._capacity = capacity

    def create(self, total: int) -> ScreenJob:
        job = ScreenJob(
            job_id=uuid.uuid4().hex,
            status="queued",
            progress=Progress(done=0, total=total),
            created_at=datetime.now(UTC),
        )
        with self._lock:
            self._jobs[job.job_id] = job
            finished = [k for k, j in self._jobs.items() if j.status in ("completed", "failed")]
            while len(self._jobs) > self._capacity and finished:
                del self._jobs[finished.pop(0)]
        return job

    def get(self, job_id: str) -> ScreenJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.model_copy(deep=True) if job else None

    def update(self, job_id: str, **changes: object) -> None:
        with self._lock:
            job = self._jobs[job_id]
            self._jobs[job_id] = job.model_copy(update=changes)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


def create_app(settings: Settings | None = None, cache: ExtractionCache | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    app = FastAPI(title="Resume screening engine", version=__version__)
    app.state.settings = settings
    app.state.jobs = JobStore(settings.max_stored_jobs)
    app.state.cache = cache if cache is not None else get_extraction_cache(settings)
    if settings.api_key is None:
        log_event(logger, "api_auth_disabled", level=logging.WARNING, hint="set API_KEY to require X-API-Key")

    def require_api_key(request: Request) -> None:
        expected = settings.api_key
        if expected is None:
            return
        given = request.headers.get("x-api-key", "")
        if not hmac.compare_digest(given.encode(), expected.get_secret_value().encode()):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid X-API-Key")

    authorized = [Depends(require_api_key)]

    @app.get("/health")
    def health() -> dict:
        try:
            extraction_mode = settings.resolved_extraction_mode
            semantic_backend = settings.resolved_semantic_backend
        except ConfigError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        return {
            "status": "ok",
            "version": __version__,
            "extraction_mode": extraction_mode,
            "semantic_backend": semantic_backend,
            "auth_required": settings.api_key is not None,
        }

    @app.post("/jobs/analyze", response_model=JobAnalysisResult, dependencies=authorized)
    def analyze_job(body: AnalyzeRequest) -> JobAnalysisResult:
        if len(body.jd_text) > settings.max_jd_chars:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, f"JD exceeds {settings.max_jd_chars} chars")
        try:
            return extract_job_requirements(body.jd_text, get_llm(settings), settings=settings)
        except ExtractionFailed as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Requirement extraction failed: {exc}") from exc

    @app.post(
        "/screen",
        response_model=ScreenAccepted,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=authorized,
    )
    async def submit_screen(
        background: BackgroundTasks,
        requirements: Annotated[str, Form(description="Reviewed JobRequirements JSON, with requirements_confirmed")],
        files: Annotated[list[UploadFile], File(description="Resumes: .pdf, .docx or .txt")],
    ) -> ScreenAccepted:
        try:
            req = JobRequirements.model_validate_json(requirements)
        except ValidationError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.errors(include_url=False)) from exc
        if req.requirements_confirmed is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "requirements_confirmed is required: HR must review the requirements before screening",
            )
        if not files:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "No files uploaded")
        if len(files) > settings.max_files_per_request:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"{len(files)} files; at most {settings.max_files_per_request} per request",
            )

        documents: list[tuple[str, bytes]] = []
        for upload in files:
            name = upload.filename or "upload"
            data = await upload.read(settings.max_file_bytes + 1)  # never buffer more than the limit + 1 byte
            try:
                detect_file_type(data, name, settings)
            except FileTooLarge as exc:
                raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, str(exc)) from exc
            except UnsupportedFileType as exc:
                raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc)) from exc
            except ParseError as exc:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
            documents.append((name, data))

        job = app.state.jobs.create(total=len(documents))
        background.add_task(_run_screening, app, job.job_id, req, documents)
        log_event(logger, "screening_job_accepted", job_id=job.job_id, documents=len(documents))
        return ScreenAccepted(
            job_id=job.job_id, status="queued", status_url=f"/screen/{job.job_id}", documents=len(documents)
        )

    @app.get("/screen/{job_id}", response_model=ScreenJob, dependencies=authorized)
    def get_screen(job_id: str) -> ScreenJob:
        job = app.state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown job {job_id!r}")
        return job

    return app


def _run_screening(app: FastAPI, job_id: str, req: JobRequirements, documents: list[tuple[str, bytes]]) -> None:
    """Background task (Starlette runs sync tasks in its thread pool)."""
    store: JobStore = app.state.jobs
    store.update(job_id, status="running")

    def progress(done: int, total: int) -> None:
        store.update(job_id, progress=Progress(done=done, total=total))

    try:
        result = screen(
            req,
            documents,
            settings=app.state.settings,
            cache=app.state.cache,
            max_workers=app.state.settings.screen_max_workers,
            progress=progress,
        )
    except Exception as exc:  # report every failure on the job instead of losing it in the background
        log_event(logger, "screening_job_failed", level=logging.ERROR, job_id=job_id, error=str(exc)[:300])
        store.update(
            job_id, status="failed", error=f"{type(exc).__name__}: {exc}"[:1000], finished_at=datetime.now(UTC)
        )
        return
    store.update(job_id, status="completed", result=result, finished_at=datetime.now(UTC))
    log_event(logger, "screening_job_completed", job_id=job_id, ranked=len(result.ranked))


app = create_app()
