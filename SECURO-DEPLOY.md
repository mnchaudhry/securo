# Deploying Securo on Render

Target: `https://securo.opstintechnologies.com` (Render plan: **Free**).

Everything is codified in [`render.yaml`](./render.yaml). The steps below are
the one-time dashboard clicks that a Blueprint can't do.

## 1. Fork + Blueprint

1. This repo is a fork of `securo-finance/securo` with three additions:
   `render.yaml`, `backend/scripts/render-entrypoint.sh`, and this file.
2. In Render: **New + → Blueprint → connect this repo**. Render reads
   `render.yaml` and creates:
   - `securo-db` (Postgres 16, pgvector-capable)
   - `securo-redis` (Key Value)
   - `securo-backend` (Docker web service, `./backend/Dockerfile`)
   - `securo-frontend` (Docker web service, `./frontend/Dockerfile`)
3. First deploy builds both images (the frontend runs `npm ci` + `vite build`).

## 2. One-time dashboard settings

- **Frontend port → 8080.** The frontend runs nginx on `:8080`; if Render routes
  to a different port, open the service → Settings → set the port to `8080`.
  (`PORT=8080` is already set in `render.yaml`; this is the belt-and-braces step.)
- **Custom domain.** Frontend service → Settings → Custom Domains → add
  `securo.opstintechnologies.com`. Then at your DNS provider add:
  ```
  CNAME  securo   securo-frontend.onrender.com
  ```
  Render issues TLS automatically once the record resolves. Click **Verify**.
- (Optional) turn off the `*.onrender.com` URL after the domain works.

## 3. First admin

Registration is disabled (`REGISTRATION_ENABLED=false`). On first visit the app
has no users, so `/setup` opens: create the first (admin) account there. That
endpoint (`POST /api/setup/create-admin`) refuses to run once a user exists.

## 4. Verify

- `GET https://securo-backend.onrender.com/api/health` → `{"status":"healthy"}`
- Frontend loads at the custom domain; log in with the setup account.
- Add a transaction to confirm writes work.

> Free web services sleep after ~15 min idle; the first request afterwards
> takes up to a minute to wake.

## 5. Data migration (later)

The BudgetFlow → Securo importer is `backend/scripts/migrate_budgetflow.py`
(source exports live in the workspace at `backups/2026-11-09/supabase-json/`).
It is **not** run yet — to be done once the deployment is confirmed live, and
after deciding how BudgetFlow "buckets" map (accounts vs categories).

Run it against Render's **external** Postgres URL from a machine with Docker:

```bash
docker run --rm \
  -e DATABASE_URL="postgresql+asyncpg://<user>:<pass>@<external-host>:5432/<db>?ssl=require" \
  -v "$PWD/backend:/app" \
  -v "$PWD/backups/2026-11-09/supabase-json:/data:ro" \
  securo-backend \
  sh -c "alembic upgrade head && python scripts/migrate_budgetflow.py --input /data --admin-email <you@example.com>"
```

The import is idempotent (users by email, categories by name, transactions by
`external_id`), so it can be re-run safely.

## Free-plan caveats (important)

- **Free Postgres expires 30 days after creation** and has no backups. Upgrade
  the database to a paid plan before relying on this.
- **No Celery worker/beat** → bank sync, recurring transactions and other
  scheduled jobs do not run. Manual finance, reports and CSV import work.
- **No persistent disks** → attachment files (none today) would not survive a
  redeploy. Use S3 (`STORAGE_S3_*`) if attachments are ever used.

## Troubleshooting

- **Migration fails on pgvector:** the backend user normally can run
  `CREATE EXTENSION`. If it can't, run once against the external URL:
  ```
  psql "<external-url>" -c 'CREATE EXTENSION IF NOT EXISTS vector;'
  ```
- **Backend 502 / crash on boot:** check the deploy logs for the
  `alembic upgrade head` step. A wrong `DATABASE_URL` scheme is the usual cause —
  it must be `postgresql+asyncpg://` (the entrypoint rewrites it, but a manually
  set value must already be correct).
- **Frontend blank / API 404s:** `BACKEND_URL` must be the backend's full public
  URL (`https://…onrender.com`), and `NGINX_RESOLVER` a public resolver (1.1.1.1).
