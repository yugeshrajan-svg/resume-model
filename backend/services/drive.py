"""Bounded downloads from public Google Drive links; never use browser cookies."""

import re
from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup

from backend.services.common import ServiceError

ID = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
EXTENSIONS = {".pdf", ".docx", ".txt"}
# A folder can span multiple import batches. Bound its listing by response size,
# independently of the number of resumes accepted in an individual batch.
MAX_FOLDER_PREVIEW_BYTES = 20_000_000
MAX_FILE_PREVIEW_BYTES = 4_000_000


def drive_link(link: str) -> tuple[str, bool]:
    url = urlparse(link)
    if url.scheme != "https" or url.netloc != "drive.google.com":
        raise ServiceError("Paste an https://drive.google.com file or folder link.", status=400)
    folder = re.search(r"/folders/([A-Za-z0-9_-]+)", url.path)
    file = re.search(r"/file/d/([A-Za-z0-9_-]+)", url.path)
    file_id = folder.group(1) if folder else file.group(1) if file else parse_qs(url.query).get("id", [""])[0]
    if not ID.fullmatch(file_id):
        raise ServiceError("This Drive link has no valid file or folder ID.", status=400)
    return file_id, bool(folder)


async def fetch_google(
    url: str, max_bytes: int, *, size_error: str = "Drive file exceeds the upload size limit."
) -> tuple[bytes, dict[str, str]]:
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        for _ in range(6):
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.netloc not in {
                "drive.google.com",
                "drive.usercontent.google.com",
                "docs.google.com",
            }:
                raise ServiceError("Drive requires sign-in. Use a link shared with anyone with the link.", status=400)
            async with client.stream("GET", url) as response:
                if response.is_redirect:
                    url = str(response.url.join(response.headers["location"]))
                    continue
                if response.status_code != 200:
                    raise ServiceError(
                        "Cannot access this Drive file. Check sharing permissions or download quota.", status=400
                    )
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                    if len(data) + len(chunk) > max_bytes:
                        raise ServiceError(size_error, status=413)
                    data.extend(chunk)
                return bytes(data), dict(response.headers)
    raise ServiceError("Drive returned too many redirects.", status=400)


def folder_files(html: bytes) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    files = {}
    for entry in soup.select(".flip-entry"):
        anchor = entry.select_one("a[href]")
        title = entry.select_one(".flip-entry-title")
        if not anchor or not title:
            continue
        try:
            file_id, folder = drive_link(anchor["href"])
        except ServiceError:
            continue
        name = title.get_text(strip=True)
        if not folder and PurePosixPath(name).suffix.lower() in EXTENSIONS:
            files[file_id] = {"id": file_id, "filename": name[:300]}
    return list(files.values())


async def preview(link: str) -> list[dict[str, str]]:
    file_id, folder = drive_link(link)
    if folder:
        html, _ = await fetch_google(
            f"https://drive.google.com/embeddedfolderview?id={file_id}#list",
            MAX_FOLDER_PREVIEW_BYTES,
            size_error=(
                "This Drive folder listing is too large to preview. Use smaller folders or individual file links."
            ),
        )
        files = folder_files(html)
    else:
        html, _ = await fetch_google(
            f"https://drive.google.com/file/d/{file_id}/view",
            MAX_FILE_PREVIEW_BYTES,
            size_error="This Drive file page is too large to preview. Download the file and upload it directly.",
        )
        soup = BeautifulSoup(html, "html.parser")
        title = soup.find("meta", property="og:title")
        name = (
            title.get("content", "")
            if title
            else soup.title.get_text().removesuffix(" - Google Drive")
            if soup.title
            else ""
        )
        files = [{"id": file_id, "filename": name[:300]}] if PurePosixPath(name).suffix.lower() in EXTENSIONS else []
    if not files:
        raise ServiceError(
            "No accessible PDF, DOCX or TXT files found. Share with anyone with the link. Subfolders are not included.",
            status=400,
        )
    return files


async def download(file_id: str, max_bytes: int) -> bytes:
    if not ID.fullmatch(file_id):
        raise ServiceError("Invalid Drive file ID.", status=400)
    data, headers = await fetch_google(
        f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t", max_bytes
    )
    if "text/html" in headers.get("content-type", ""):
        raise ServiceError(
            "Drive did not provide a downloadable file. Check sharing permissions and quota.", status=400
        )
    return data
