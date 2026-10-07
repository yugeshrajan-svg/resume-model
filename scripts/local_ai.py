"""Install and run free local models without an API key."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CACHE = ROOT / ".cache" / "local-ai"
BASE = "http://127.0.0.1:11434"
RUNTIME_VERSION = "v0.35.1"
RUNTIME_SHA = "dc50b9ca7f9023c86525012632cd1615b093d0407987444a7f62ecab617e8e93"
OCR_SHA = "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2"
CHAT_MODEL = "shortlist-local:v1"
BASE_MODEL = "qwen3:1.7b"
EMBED_MODEL = "nomic-embed-text:v1.5"


def download(url: str, destination: Path, digest: str) -> None:
    if destination.is_file():
        with destination.open("rb") as existing:
            if hashlib.file_digest(existing, "sha256").hexdigest() == digest:
                return
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    checksum = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=60) as response, partial.open("wb") as target:
        size = int(response.headers.get("Content-Length", "0"))
        received, last = 0, -1
        while block := response.read(1024 * 1024):
            target.write(block)
            checksum.update(block)
            received += len(block)
            percent = received * 100 // size if size else 0
            if percent // 10 > last:
                last = percent // 10
                print(f"Downloading {destination.name}: {percent}%", flush=True)
    if checksum.hexdigest() != digest:
        raise RuntimeError(f"Checksum mismatch for {destination.name}; downloaded file was not installed")
    partial.replace(destination)


def executable(*, install: bool = False) -> str:
    installed = shutil.which("ollama")
    if installed:
        return installed
    portable = CACHE / "runtime" / "ollama.exe"
    if portable.is_file():
        return str(portable)
    if not install or os.name != "nt":
        raise RuntimeError("Install Ollama from https://ollama.com/download, then run setup")
    archive = CACHE / f"ollama-{RUNTIME_VERSION}.zip"
    download(
        f"https://github.com/ollama/ollama/releases/download/{RUNTIME_VERSION}/ollama-windows-amd64.zip",
        archive,
        RUNTIME_SHA,
    )
    folder = portable.parent.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as package:
        for member in package.infolist():
            if not (folder / member.filename).resolve().is_relative_to(folder):
                raise RuntimeError("Unsafe runtime archive path")
        package.extractall(folder)
    return str(portable)


def runtime_environment() -> dict[str, str]:
    from engine.config import Settings

    settings = Settings()  # reads .env: capacity can be raised on a machine with a GPU
    # An existing Ollama installation normally stores its models in the user's .ollama directory.
    default_models = Path.home() / ".ollama" / "models" if shutil.which("ollama") else CACHE / "models"
    return {
        **os.environ,
        "OLLAMA_HOST": "127.0.0.1:11434",
        "OLLAMA_MODELS": os.environ.get("OLLAMA_MODELS", str(default_models)),
        "OLLAMA_NO_CLOUD": "1",
        "OLLAMA_NUM_PARALLEL": str(settings.ollama_num_parallel),
        "OLLAMA_MAX_LOADED_MODELS": str(settings.ollama_max_loaded_models),
        "OLLAMA_FLASH_ATTENTION": "1",
        "OLLAMA_KV_CACHE_TYPE": "q8_0",
    }


def available() -> bool:
    try:
        with httpx.Client(trust_env=False, timeout=2) as client:
            response = client.get(BASE + "/api/tags")
            return response.status_code == 200 and isinstance(response.json().get("models"), list)
    except (httpx.HTTPError, ValueError):
        return False


def stream_request(path: str, body: dict) -> None:
    with (
        httpx.Client(trust_env=False, timeout=900) as client,
        client.stream("POST", BASE + path, json=body) as response,
    ):
        response.raise_for_status()
        previous = None
        success = False
        for line in response.iter_lines():
            event = json.loads(line)
            if event.get("error"):
                raise RuntimeError("Local model installation failed; check the Ollama log")
            total = event.get("total", 0)
            progress = int(event.get("completed", 0) * 100 / total) if total else None
            status = event.get("status", "")
            summary = (status, progress // 10 if progress is not None else None)
            if summary != previous:
                print(f"{status}" + (f": {progress}%" if progress is not None else ""), flush=True)
                previous = summary
            success = status == "success"
        if not success:
            raise RuntimeError("Local model installation ended before success")


def stop_owned(process: subprocess.Popen) -> None:
    """Stop only the server this helper started, including its model runners on Windows."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        process.terminate()
    process.wait(timeout=30)


def setup(*, rebuild: bool = False) -> None:
    command = executable(install=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    process = None
    try:
        if not available():
            with (CACHE / "ollama.log").open("ab") as log:
                process = subprocess.Popen(
                    [command, "serve"],
                    env=runtime_environment(),
                    cwd=ROOT,
                    stdout=log,
                    stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            for _ in range(60):
                if available():
                    break
                if process.poll() is not None:
                    raise RuntimeError("Ollama could not start; check .cache/local-ai/ollama.log")
                time.sleep(1)
            else:
                raise RuntimeError("Timed out starting Ollama")
        with httpx.Client(trust_env=False, timeout=10) as client:
            names = {item["name"] for item in client.get(BASE + "/api/tags").json()["models"]}
        for model in (BASE_MODEL, EMBED_MODEL):
            if rebuild or model not in names:
                print(f"Installing {model}", flush=True)
                stream_request("/api/pull", {"model": model})
        if rebuild or CHAT_MODEL not in names:
            subprocess.run(
                [command, "create", CHAT_MODEL, "-f", str(ROOT / "models" / "shortlist-local.Modelfile")],
                env=runtime_environment(),
                check=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        download(
            "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/87416418657359cb625c412a48b6e1d6d41c29bd/eng.traineddata",
            CACHE / "tessdata" / "eng.traineddata",
            OCR_SHA,
        )
        with httpx.Client(trust_env=False, timeout=10) as client:
            report = {item["name"]: item["digest"] for item in client.get(BASE + "/api/tags").json()["models"]}
        (CACHE / "installed-models.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print("Setup complete. Run: python scripts/local_ai.py serve", flush=True)
    finally:
        if process is not None:
            stop_owned(process)


def serve() -> int:
    if available():
        print("Local Ollama is already running. This helper will leave that server running.")
        return 0
    CACHE.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        import ctypes

        # Some terminal launchers inherit the Windows ignore-Ctrl+C flag into their child processes.
        ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)
    with (CACHE / "ollama.log").open("ab") as log:
        process = subprocess.Popen(
            [executable(), "serve"],
            env=runtime_environment(),
            cwd=ROOT,
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            print("Starting local AI at 127.0.0.1:11434. Press Ctrl+C to stop. Logs: .cache/local-ai/ollama.log")
            # Timed waits let Python handle Ctrl+C while the Windows child has no console of its own.
            while True:
                try:
                    return process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    continue
        except KeyboardInterrupt:
            return 0
        finally:
            stop_owned(process)


def smoke() -> None:
    import pymupdf

    from engine.config import Settings
    from engine.extraction import extract_candidate, extract_job_requirements
    from engine.llm import get_llm
    from engine.local_ai import OllamaProvider, pin_local_models
    from engine.parsing import parse_bytes, parse_file

    settings = Settings(
        _env_file=None,
        LLM_PROVIDER="ollama",
        EXTRACTION_MODE="llm",
        SEMANTIC_BACKEND="embeddings",
        OCR_PROVIDER="tesseract",
        LLM_TIMEOUT_S=600,
        LLM_MAX_RETRIES=0,
        EXTRACTION_CACHE_ENABLED=False,
        TESSERACT_DATA_DIR=CACHE / "tessdata",
    )
    settings = pin_local_models(settings)
    with pymupdf.open() as original, pymupdf.open() as scanned:
        page = original.new_page()
        page.insert_text((60, 80), "Python SQL backend developer resume", fontsize=18)
        scan = scanned.new_page()
        scan.insert_image(scan.rect, pixmap=page.get_pixmap(dpi=200))
        ocr = parse_bytes(scanned.tobytes(deflate=True), "synthetic-scan.pdf", settings=settings)
    if not ocr.ocr_applied or "Python" not in ocr.text:
        raise RuntimeError("Local OCR smoke check failed")
    resume = parse_file(ROOT / "data" / "sample" / "resumes" / "01_strong_match.pdf", settings=settings)
    llm = get_llm(settings)
    candidate = extract_candidate(resume.text, llm=llm, settings=settings)
    requirements = extract_job_requirements(
        "Python Backend Developer\nRequired skills: Python, SQL\nLocation: Remote", llm=llm, settings=settings
    )
    embeddings = OllamaProvider(settings).embed(["Python SQL backend developer", "Accountant payroll bookkeeping"])
    report = {
        "chat_model": settings.chat_model,
        "chat_digest": settings.ollama_chat_digest,
        "candidate_status": candidate.status,
        "verified_skills": [s.name for s in candidate.extraction.skills] if candidate.extraction else [],
        "required_skills": [s.name for s in requirements.requirements.required_skills],
        "vectors": len(embeddings.vectors),
        "vector_dimensions": len(embeddings.vectors[0]),
        "embedding_api_cost_usd": embeddings.usage.cost_usd,
        "chat_api_cost_usd": candidate.usage.cost_usd if candidate.usage else None,
        "ocr_applied": ocr.ocr_applied,
        "ocr_pages": ocr.ocr_pages,
    }
    print(json.dumps(report, indent=2))
    if candidate.status != "ok" or not report["verified_skills"] or not report["required_skills"]:
        raise RuntimeError("Local extraction failed validation")


def processor(model: dict) -> str:
    """Where a loaded model runs, from Ollama's /api/ps sizes: GPU, CPU or a split."""
    size, vram = model.get("size") or 0, model.get("size_vram") or 0
    if not size or not vram:
        return "CPU"
    return "GPU" if vram >= size else f"{round(100 * vram / size)}% GPU / {100 - round(100 * vram / size)}% CPU"


def status() -> None:
    if not available():
        print("Local AI: stopped")
        return
    env = runtime_environment()
    print(
        f"Local AI: running (parallel requests {env['OLLAMA_NUM_PARALLEL']}, "
        f"loaded models {env['OLLAMA_MAX_LOADED_MODELS']}; restart serve after changing them)"
    )
    with httpx.Client(trust_env=False, timeout=5) as client:
        loaded = client.get(BASE + "/api/ps").json().get("models", [])
    if not loaded:
        print("No model loaded yet: run a screening or `smoke`, then check again to see GPU or CPU use.")
    for model in loaded:
        print(f"  {model.get('name')}: {processor(model)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("setup", "serve", "status", "smoke"))
    parser.add_argument("--rebuild", action="store_true", help="Refresh models; create new screening runs afterwards")
    args = parser.parse_args()
    os.chdir(ROOT)
    try:
        if args.action == "setup":
            setup(rebuild=args.rebuild)
        elif args.action == "serve":
            return serve()
        elif args.action == "smoke":
            smoke()
        else:
            status()
        return 0
    except (OSError, RuntimeError, httpx.HTTPError, subprocess.SubprocessError) as exc:
        print(f"Local AI setup/check failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
