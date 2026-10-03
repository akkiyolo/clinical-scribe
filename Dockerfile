# ClinicalScribe container image (Python 3.11, FastAPI + Uvicorn).
#
#   docker build -t clinicalscribe .
#   docker run --rm -p 8000:8000 --env-file .env -e DATABASE_URL=... clinicalscribe
#
# Configuration comes from environment variables (see .env.example). The .env file is NOT copied
# into the image, so no secret is ever baked into a layer.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so this layer is cached until requirements.txt changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY scripts ./scripts

# Run as an unprivileged user. /app/.local_storage is only used when STORAGE_BACKEND=local (development).
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/.local_storage \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Render and similar platforms inject $PORT; locally it defaults to 8000.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8000'), timeout=4)"

# Apply migrations, bootstrap the first admin, then serve (same entrypoint as render.yaml).
CMD ["sh", "scripts/start.sh"]
