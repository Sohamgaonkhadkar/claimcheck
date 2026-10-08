# Runbook

This is the source-tree development workflow. It is not a production operations/SLO guide. Current release gate: frontend/backend architecture is frozen; PostgreSQL integration/runtime and real-browser E2E still need verification.

## Prerequisites

- Python **3.11+** (the container images use Python 3.12).
- Node **20.19+ or 22.12+** for the checked-in Vite 7 toolchain.
- Docker Compose and a disposable/local PostgreSQL service for API/worker runtime.
- The `tesseract-ocr` system binary for OCR in the worker. On Debian/Ubuntu: `sudo apt-get install -y tesseract-ocr`; on macOS: `brew install tesseract`.

## Local setup

From the repository root:

```bash
cp .env.example .env
```

Edit `.env` first: replace the local placeholder password with a random password and update both `POSTGRES_PASSWORD` and the matching password in `DATABASE_URL`. Never commit `.env`. The default Compose credentials and fixed server-side identity are for local development only.

Create a Python environment and install declared development/test dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

Start the local PostgreSQL service and export the same variables for Alembic, API, and worker processes:

```bash
docker compose up -d db
set -a
. ./.env
set +a
python -m alembic heads
python -m alembic upgrade b31f5c2a9e70
```

Apply migrations explicitly before starting the app; neither the API nor worker creates production tables at startup. The checked-in migration chain has one head, `b31f5c2a9e70`. Keep the database disposable, private, and separate from any real/customer data.

## Start the processes

In separate shells with the virtual environment active and the same exported `.env` settings:

```bash
uvicorn claimcheck.api.app:app --host 127.0.0.1 --port 8000
```

```bash
claimcheck-worker
```

Check liveness and readiness:

```bash
curl -i http://127.0.0.1:8000/healthz
curl -i http://127.0.0.1:8000/readyz
```

`/healthz` is liveness only. `/readyz` requires a reachable PostgreSQL database and writable private storage. The worker must share the API's database and storage root; queued uploads/deletions do not complete while no worker is running.

## Start the frontend

In another shell:

```bash
cd web
npm ci
npm run dev
```

Vite serves on port `5173` and proxies same-origin `/api` requests to the local API at port `8000`. This local proxy means CORS can remain unset. The dev identity represents one fixed local owner; it is not user authentication.

To verify the production frontend bundle:

```bash
npm run build
```

This writes `web/dist/`; it is generated output and must not be committed. `npm run build` runs TypeScript checking and Vite production bundling; it does not perform browser E2E.

## Focused non-PostgreSQL checks

The repository includes data-backed research tests that require excluded corpus/annotation assets. For a public source checkout, run the self-contained product and configuration checks rather than treating the entire research suite as a clean-install smoke test:

```bash
python -m pytest -q \
  tests/test_phase2_cors.py \
  tests/test_phase2_safe_api.py \
  tests/test_phase2_storage_and_upload_validation.py \
  tests/test_api_vertical_slice.py \
  tests/test_phase4_review_readiness.py \
  tests/test_phase5_analysis.py \
  tests/test_reproducibility.py
```

Run this PostgreSQL integration set **only** against a disposable PostgreSQL database. These modules apply migrations and create/delete test data; never point them at production, a shared development DB, or a database with user data:

```bash
CLAIMCHECK_TEST_DATABASE_URL='postgresql+psycopg://TEST_USER:TEST_SECRET@127.0.0.1:5432/claimcheck_test' \
  python -m pytest -q \
  tests/test_phase2_backend_api.py \
  tests/test_phase3_worker_postgres.py \
  tests/test_phase4_review_trusted_adapter_postgres.py
```

## Local container builds

The API and worker are separate processes/images over the same frozen application architecture:

```bash
docker build -f Dockerfile.api -t claimcheck-api:local .
docker build -f Dockerfile.worker -t claimcheck-worker:local .
```

`Dockerfile.api` exposes port `8000`; its image health check calls `/healthz`. Configure the platform readiness probe separately to call `/readyz`. `Dockerfile.worker` runs the existing `claimcheck-worker` entrypoint and installs the Tesseract OCR system package. Both images run as UID/GID `10001`; mount private storage with permissions that allow that identity. These commands have not been asserted as successful unless a Docker build is actually run in the target environment.

## Common failure checks

- **`DATABASE_URL` missing/invalid:** export the PostgreSQL URL into the API, worker, and Alembic shells. Production URLs must use PostgreSQL (`postgresql+psycopg://`); SQLite is unsupported.
- **`/readyz` returns `503`:** check PostgreSQL reachability/migrations and ensure `CLAIMCHECK_STORAGE_ROOT` is writable by the API identity. Readiness intentionally does not return physical paths.
- **Documents remain queued:** run one or more workers, check that API and worker use the same DB/storage, and inspect job status/retry fields.
- **Scanned pages extract no text:** install Tesseract and keep `CLAIMCHECK_OCR_ENABLED=true`; OCR wrapper installation alone does not provide the system executable.
- **Frontend API requests fail:** keep relative `/api` calls and the Vite proxy for local development; do not point browser code at `localhost` in a deployed build. For a separately hosted production UI, configure exact CORS origins and a production identity integration.
- **CORS rejected:** set `CLAIMCHECK_CORS_ORIGINS` to a comma-separated list of exact HTTPS origins. HTTP is accepted only for localhost in development; wildcards and credentials are not supported.

## Operational caution

Do not use local development identity, sample Compose credentials, a local filesystem mount without private access controls, research fixtures, or the development-only Golden helper for real customer claims. No PostgreSQL runtime, browser E2E, or AWS deployment result should be inferred from a passing unit test or image-build command.
