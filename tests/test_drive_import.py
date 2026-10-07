import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.api import routes_drive
from backend.api.deps import Services
from backend.config import PlatformSettings
from backend.main import create_app
from backend.security import Principal, issue_access_token
from backend.services import drive
from backend.services.common import ServiceError
from backend.storage import InMemoryBlobStorage


@pytest.mark.parametrize(
    "url",
    [
        "http://drive.google.com/file/d/abcdefghijk/view",
        "https://drive.google.com.evil.test/file/d/abcdefghijk/view",
        "https://127.0.0.1/file/d/abcdefghijk/view",
        "https://drive.google.com/file/d/abc/view",
    ],
)
def test_rejects_non_drive_links(url):
    with pytest.raises(ServiceError):
        drive.drive_link(url)


def test_folder_lists_supported_files_once_and_skips_subfolders():
    html = b"""<div class="flip-entry"><a href="https://drive.google.com/file/d/abcdefghijk/view">
    <div class="flip-entry-title">Resume.PDF</div></a></div>
    <div class="flip-entry"><a href="https://drive.google.com/file/d/abcdefghijk/view">
    <div class="flip-entry-title">Resume.PDF</div></a></div>
    <div class="flip-entry"><a href="https://drive.google.com/drive/folders/folderabcdefgh">
    <div class="flip-entry-title">Nested.pdf</div></a></div>
    <div class="flip-entry"><a href="https://evil.test/file/d/abcdefghijk/view">
    <div class="flip-entry-title">Evil.pdf</div></a></div>"""
    assert drive.folder_files(html) == [{"id": "abcdefghijk", "filename": "Resume.PDF"}]


def test_preview_large_folder_returns_every_resume_in_listing_order(monkeypatch):
    files = [{"id": f"resume{i:010d}", "filename": f"Resume {i}.pdf"} for i in range(1201)]
    html = "".join(
        f'<div class="flip-entry"><a href="https://drive.google.com/file/d/{item["id"]}/view">'
        f'<div class="flip-entry-title">{item["filename"]}</div></a></div>'
        for item in files
    ).encode()
    fetch = AsyncMock(return_value=(html, {"content-type": "text/html"}))
    monkeypatch.setattr(drive, "fetch_google", fetch)

    assert asyncio.run(drive.preview("https://drive.google.com/drive/folders/folderabcdefgh")) == files
    assert fetch.await_args.args[1] == drive.MAX_FOLDER_PREVIEW_BYTES


def test_folder_preview_keeps_response_size_bounded_with_actionable_error(monkeypatch):
    factory = httpx.AsyncClient
    monkeypatch.setattr(drive, "MAX_FOLDER_PREVIEW_BYTES", 5)
    monkeypatch.setattr(
        drive.httpx,
        "AsyncClient",
        lambda **kwargs: factory(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"123456")), **kwargs
        ),
    )
    with pytest.raises(ServiceError, match="folder listing is too large") as error:
        asyncio.run(drive.preview("https://drive.google.com/drive/folders/folderabcdefgh"))
    assert error.value.status == 413


def test_google_stream_limit_and_redirect_allowlist(monkeypatch):
    factory = httpx.AsyncClient

    def run(handler):
        monkeypatch.setattr(
            drive.httpx, "AsyncClient", lambda **kwargs: factory(transport=httpx.MockTransport(handler), **kwargs)
        )
        return asyncio.run(drive.fetch_google("https://drive.google.com/example", 5))

    with pytest.raises(ServiceError, match="size limit"):
        run(lambda request: httpx.Response(200, content=b"123456"))
    with pytest.raises(ServiceError, match="sign-in"):
        run(lambda request: httpx.Response(302, headers={"location": "http://127.0.0.1/private"}))
    assert run(lambda request: httpx.Response(200, content=b"12345"))[0] == b"12345"


def test_html_download_is_not_a_resume(monkeypatch):
    monkeypatch.setattr(drive, "fetch_google", AsyncMock(return_value=(b"login", {"content-type": "text/html"})))
    with pytest.raises(ServiceError, match="downloadable"):
        asyncio.run(drive.download("abcdefghijk", 100))


def test_import_and_original_download_permissions(monkeypatch):
    settings = PlatformSettings(_env_file=None, APP_ENV="test", JWT_SECRET="test-key-" * 5)
    svc = Services(settings, AsyncMock(), InMemoryBlobStorage(), AsyncMock())
    monkeypatch.setattr("backend.main.require_ready", AsyncMock())
    preview = AsyncMock(return_value=[{"id": "abcdefghijk", "filename": "resume.pdf"}])
    monkeypatch.setattr(drive, "preview", preview)
    download = AsyncMock()
    monkeypatch.setattr(drive, "download", download)
    import_body = {"file_id": "abcdefghijk", "filename": "resume.pdf"}
    with TestClient(create_app(settings, svc=svc)) as client:
        assert client.post("/v1/drive/preview", json={"link": "https://drive.google.com/example"}).status_code == 401
        assert client.post("/v1/drive/import", json=import_body).status_code == 401
        principal = Principal(uuid.uuid4(), uuid.uuid4(), "hiring_manager")
        token, _ = issue_access_token(principal, settings)
        headers = {"Authorization": f"Bearer {token}"}
        assert (
            client.post(
                "/v1/drive/preview", json={"link": "https://drive.google.com/example"}, headers=headers
            ).status_code
            == 403
        )
        assert client.post("/v1/drive/import", json=import_body, headers=headers).status_code == 403
        assert client.get(f"/v1/resumes/{uuid.uuid4()}/download", headers=headers).status_code == 403
        assert preview.await_count == 0
        download.assert_not_awaited()
        principal = Principal(principal.user_id, principal.company_id, "recruiter")
        token, _ = issue_access_token(principal, settings)
        response = client.post(
            "/v1/drive/preview",
            json={"link": "https://drive.google.com/example"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json()["files"][0]["filename"] == "resume.pdf"


@pytest.mark.parametrize("with_job", [True, False])
def test_import_registers_bytes_and_cleans_failed_upload(monkeypatch, with_job):
    storage = InMemoryBlobStorage()
    repo = AsyncMock()
    principal = Principal(uuid.uuid4(), uuid.uuid4(), "recruiter")

    @asynccontextmanager
    async def tenant(_principal):
        yield repo

    svc = SimpleNamespace(storage=storage, queue=AsyncMock(), tenant=tenant)
    resume = SimpleNamespace(
        id=uuid.uuid4(),
        candidate_id=None,
        version=None,
        filename="resume.txt",
        mime="text/plain",
        size=12,
        status="registered",
        status_reason=None,
        processing_generation=1,
        processed_generation=0,
        created_at=datetime.now(UTC),
    )
    register = AsyncMock(return_value=(resume, True))
    monkeypatch.setattr(routes_drive, "register_resume", register)
    monkeypatch.setattr(drive, "download", AsyncMock(return_value=b"resume bytes"))
    association = (
        {"job_id": uuid.uuid4()}
        if with_job
        else {"consent": {"source": "Recruiter confirmation", "purpose": "Future roles", "scope": "talent_pool"}}
    )
    body = routes_drive.DriveImportIn(file_id="abcdefghijk", filename="resume.txt", **association)
    result = asyncio.run(routes_drive.import_drive(body, principal, svc))
    assert result.created and result.resume.id == resume.id
    item = register.call_args.args[-1]
    assert storage.blobs[item.blob_name] == b"resume bytes"
    assert item.job_id == body.job_id
    if with_job:
        repo.get.assert_awaited_once_with(routes_drive.Job, body.job_id)
        assert item.consent is None
    else:
        repo.get.assert_not_awaited()
        assert item.job_id is None
        assert item.consent == {**association["consent"], "granted_at": None}
    before = dict(storage.blobs)
    register.side_effect = ServiceError("Rejected bytes", status=400)
    with pytest.raises(ServiceError, match="Rejected"):
        asyncio.run(routes_drive.import_drive(body, principal, svc))
    assert storage.blobs == before
