# Resume screening engine API (offline-capable; Azure OpenAI is used when credentials are provided).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    EXTRACTION_CACHE_DIR=/app/.cache/extractions

WORKDIR /app

COPY pyproject.toml requirements.txt README.md ./

COPY engine ./engine
COPY api ./api
COPY backend ./backend
COPY alembic.ini ./
COPY migrations ./migrations
COPY data/sample ./data/sample
RUN pip install --no-cache-dir .

# Run as an unprivileged user; only the cache directory is writable.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/.cache/extractions \
    && chown -R app:app /app/.cache
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status != 200)"

# One worker: screening jobs are tracked in process memory (see api/main.py).
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
