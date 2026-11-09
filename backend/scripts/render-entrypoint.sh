#!/bin/sh
# Render entrypoint for the Securo backend.
#
# Two jobs:
#   1. Normalise DATABASE_URL: Render injects it as `postgresql://...`, but
#      SQLAlchemy needs the async driver (`postgresql+asyncpg://...`).
#   2. Apply migrations, then serve. Free Render plans have no preDeployCommand,
#      so migrations run here, on boot, right before uvicorn starts.
set -e

case "${DATABASE_URL:-}" in
  postgresql://*)
    DATABASE_URL="postgresql+asyncpg://${DATABASE_URL#postgresql://}"
    export DATABASE_URL
    ;;
esac

echo "[render-entrypoint] applying migrations (alembic upgrade head)"
alembic upgrade head

echo "[render-entrypoint] starting uvicorn on port ${PORT:-8000}"
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
