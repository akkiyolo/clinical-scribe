#!/bin/sh
# Production entrypoint (Render and the Docker image): migrate, bootstrap the first admin, serve.
#
# - Schema adoption and migrations are idempotent, so running them on every start is safe.
# - create_admin is idempotent too. It runs here because Render's free plan has no shell or
#   pre-deploy command; a failure (e.g. a weak ADMIN_BOOTSTRAP_PASSWORD) is logged but does not
#   stop the web server from starting.
# - --proxy-headers plus FORWARDED_ALLOW_IPS (read by uvicorn) make it use the client IP from
#   Render's proxy (X-Forwarded-For), so per-IP rate limits apply per client, not to the proxy.
# - One worker only: background jobs run in-process (see app/services/jobs.py).
set -e

# A database created outside Alembic (all tables, no alembic_version) is stamped first so the
# migrations don't try to recreate it. No-op on fresh or already-managed databases.
python -m scripts.adopt_schema
alembic upgrade head

python -m scripts.create_admin || echo "WARNING: admin bootstrap skipped (see the error above)"

export FORWARDED_ALLOW_IPS="${FORWARDED_ALLOW_IPS:-*}"
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers
